import time
from typing import TYPE_CHECKING, List, Optional, Union, Dict, Any
from decimal import Decimal

import attr

from .json_db import StoredObject
from .i18n import _
from .util import age, InvoiceError, Satoshis
from .lnaddr import lndecode, LnAddr
from . import constants
from .ravencoin import COIN, TOTAL_COIN_SUPPLY_LIMIT_IN_BTC
from .transaction import PartialTxOutput, RavenValue

if TYPE_CHECKING:
    from .paymentrequest import PaymentRequest

# convention: 'invoices' = outgoing , 'request' = incoming

# types of payment requests
PR_TYPE_ONCHAIN = 0
PR_TYPE_LN = 2

# status of payment requests
PR_UNPAID   = 0
PR_EXPIRED  = 1
PR_UNKNOWN  = 2     # sent but not propagated
PR_PAID     = 3     # send and propagated
PR_INFLIGHT = 4     # unconfirmed
PR_FAILED   = 5
PR_ROUTING  = 6
PR_UNCONFIRMED = 7

pr_color = {
    PR_UNPAID:   (.7, .7, .7, 1),
    PR_PAID:     (.2, .9, .2, 1),
    PR_UNKNOWN:  (.7, .7, .7, 1),
    PR_EXPIRED:  (.9, .2, .2, 1),
    PR_INFLIGHT: (.9, .6, .3, 1),
    PR_FAILED:   (.9, .2, .2, 1),
    PR_ROUTING: (.9, .6, .3, 1),
    PR_UNCONFIRMED: (.9, .6, .3, 1),
}

pr_tooltips = {
    PR_UNPAID:_('Unpaid'),
    PR_PAID:_('Paid'),
    PR_UNKNOWN:_('Unknown'),
    PR_EXPIRED:_('Expired'),
    PR_INFLIGHT:_('In progress'),
    PR_FAILED:_('Failed'),
    PR_ROUTING: _('Computing route...'),
    PR_UNCONFIRMED: _('Unconfirmed'),
}

PR_DEFAULT_EXPIRATION_WHEN_CREATING = 24*60*60  # 1 day
pr_expiration_values = {
    0: _('Never'),
    10*60: _('10 minutes'),
    60*60: _('1 hour'),
    24*60*60: _('1 day'),
    7*24*60*60: _('1 week'),
}
assert PR_DEFAULT_EXPIRATION_WHEN_CREATING in pr_expiration_values


def _decode_outputs(outputs) -> List[PartialTxOutput]:
    ret = []
    for output in outputs:
        if not isinstance(output, PartialTxOutput):
            try:
                output = PartialTxOutput.from_legacy_tuple(*output)
            except:
                continue
        ret.append(output)
    return ret


# hack: BOLT-11 is not really clear on what an expiry of 0 means.
# It probably interprets it as 0 seconds, so already expired...
# Our higher level invoices code however uses 0 for "never".
# Hence set some high expiration here
LN_EXPIRY_NEVER = 100 * 365 * 24 * 60 * 60  # 100 years

@attr.s
class Invoice(StoredObject):
    type = attr.ib(type=int, kw_only=True)

    message: str
    exp: int
    time: int

    def is_lightning(self):
        return self.type == PR_TYPE_LN

    def get_status_str(self, status):
        status_str = pr_tooltips[status]
        if status == PR_UNPAID:
            if self.exp > 0 and self.exp != LN_EXPIRY_NEVER:
                expiration = self.exp + self.time
                status_str = _('Expires') + ' ' + age(expiration, include_seconds=True)
        return status_str

    def get_amount_sat(self) -> Union[RavenValue, str, None]:
        """Returns a decimal satoshi amount, or '!' or None."""
        raise NotImplementedError()

    @classmethod
    def from_json(cls, x: dict) -> 'Invoice':
        # note: these raise if x has extra fields
        if x.get('type') == PR_TYPE_LN:
            return LNInvoice(**x)
        else:
            return OnchainInvoice(**x)


@attr.s
class OnchainInvoice(Invoice):
    message = attr.ib(type=str, kw_only=True)
    amount_sat = attr.ib(kw_only=True)  # type: RavenValue
    exp = attr.ib(type=int, kw_only=True, validator=attr.validators.instance_of(int))
    time = attr.ib(type=int, kw_only=True, validator=attr.validators.instance_of(int))
    id = attr.ib(type=str, kw_only=True)
    outputs = attr.ib(kw_only=True, converter=_decode_outputs)  # type: List[PartialTxOutput]
    bip70 = attr.ib(type=str, kw_only=True)  # type: Optional[str]
    requestor = attr.ib(type=str, kw_only=True)  # type: Optional[str]
    height = attr.ib(type=int, kw_only=True, validator=attr.validators.instance_of(int))

    def get_address(self) -> str:
        """returns the first address, to be displayed in GUI"""
        return self.outputs[0].address

    def get_amount_sat(self) -> RavenValue:
        return self.amount_sat or RavenValue()

    @amount_sat.validator
    def _validate_amount(self, attribute, value):
        if isinstance(value, int):
            self.amount_sat = value = RavenValue(value)
        elif isinstance(value, Dict):
            self.amount_sat = value = RavenValue.from_json(value)
        if isinstance(value, RavenValue):
            if not (0 <= value.rvn_value <= TOTAL_COIN_SUPPLY_LIMIT_IN_BTC * COIN):
                raise InvoiceError(f"amount is out-of-bounds: {value!r} sat")
        elif isinstance(value, str):
            if value != "!":
                raise InvoiceError(f"unexpected amount: {value!r}")
        else:
            raise InvoiceError(f"unexpected amount: {value!r}")

    @classmethod
    def from_bip70_payreq(cls, pr: 'PaymentRequest', height:int) -> 'OnchainInvoice':
        return OnchainInvoice(
            type=PR_TYPE_ONCHAIN,
            amount_sat=pr.get_amount(),
            outputs=pr.get_outputs(),
            message=pr.get_memo(),
            id=pr.get_id(),
            time=pr.get_time(),
            exp=pr.get_expiration_date() - pr.get_time(),
            bip70=pr.raw.hex(),
            requestor=pr.get_requestor(),
            height=height,
        )

@attr.s
class LNInvoice(Invoice):
    invoice = attr.ib(type=str)
    amount_msat = attr.ib(kw_only=True)  # type: Optional[int]  # needed for zero amt invoices

    __lnaddr = None

    @invoice.validator
    def _validate_invoice_str(self, attribute, value):
        lndecode(value)  # this checks the str can be decoded

    @amount_msat.validator
    def _validate_amount(self, attribute, value):
        if value is None:
            return
        if isinstance(value, int):
            if not (0 <= value <= TOTAL_COIN_SUPPLY_LIMIT_IN_BTC * COIN * 1000):
                raise InvoiceError(f"amount is out-of-bounds: {value!r} msat")
        else:
            raise InvoiceError(f"unexpected amount: {value!r}")

    @property
    def _lnaddr(self) -> LnAddr:
        if self.__lnaddr is None:
            self.__lnaddr = lndecode(self.invoice)
        return self.__lnaddr

    @property
    def rhash(self) -> str:
        return self._lnaddr.paymenthash.hex()

    def get_amount_msat(self) -> Optional[int]:
        amount_btc = self._lnaddr.amount
        amount = int(amount_btc * COIN * 1000) if amount_btc else None
        return amount or self.amount_msat

    def get_amount_sat(self) -> Union[RavenValue, None]:
        amount_msat = self.get_amount_msat()
        if amount_msat is None:
            return None
        return RavenValue(Satoshis(Decimal(amount_msat) / 1000))

    @property
    def exp(self) -> int:
        return self._lnaddr.get_expiry()

    @property
    def time(self) -> int:
        return self._lnaddr.date

    @property
    def message(self) -> str:
        return self._lnaddr.get_description()

    @classmethod
    def from_bech32(cls, invoice: str) -> 'LNInvoice':
        """Constructs LNInvoice object from BOLT-11 string.
        Might raise InvoiceError.
        """
        try:
            lnaddr = lndecode(invoice)
        except Exception as e:
            raise InvoiceError(e) from e
        amount_msat = lnaddr.get_amount_msat()
        return LNInvoice(
            type=PR_TYPE_LN,
            invoice=invoice,
            amount_msat=amount_msat,
        )

    def to_debug_json(self) -> Dict[str, Any]:
        d = self.to_json()
        d.update({
            'pubkey': self._lnaddr.pubkey.serialize().hex(),
            'amount_BTC': str(self._lnaddr.amount),
            'rhash': self._lnaddr.paymenthash.hex(),
            'description': self._lnaddr.get_description(),
            'exp': self._lnaddr.get_expiry(),
            'time': self._lnaddr.date,
            # 'tags': str(lnaddr.tags),
        })
        return d
