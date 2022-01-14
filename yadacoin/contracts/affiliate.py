import base64
import time
import binascii
import base58
import hashlib
from enum import Enum
from bitcoin.signmessage import BitcoinMessage, VerifyMessage
from bitcoin.wallet import P2PKHBitcoinAddress
from coincurve.utils import verify_signature

from yadacoin.contracts.base import (
    Contract,
    ContractTypes,
    PayoutOperators,
    PayoutType
)
from yadacoin.core.collections import Collections
from yadacoin.core.identity import Identity, PrivateIdentity
from yadacoin.core.transaction import (
    InvalidTransactionException,
    InvalidTransactionSignatureException,
    Output
)


class AffiliatePoofTypes(Enum):
    CONFIRMATION = 'confirmation'
    HONOR = 'honor'


class ReferPayout:
    def __init__(
      self,
      active=False,
      operator='',
      payout_type='',
      interval='',
      amount=''
    ):

        if active is True:

            if operator not in [x.value for x in PayoutOperators]:
                self.report_init_error('operator')

            if payout_type not in [x.value for x in PayoutType]:
                self.report_init_error('payout_type')

            if (
                payout_type == PayoutType.RECURRING.value and
                not isinstance(interval, float) and
                not isinstance(interval, int)
            ):
                self.report_init_error('interval')

            if not isinstance(amount, float) and not isinstance(amount, int):
                self.report_init_error('amount')

        self.active = active
        self.operator = operator
        self.payout_type = payout_type
        self.interval = interval
        self.amount = amount

    def report_init_error(self, member):
        raise Exception(f'Cannot instantiate referpayout with invalid {member}')

    def get_string(self, p):
        return '' if p is None else str(p)

    def to_dict(self):
        return {
            'active': self.active,
            'operator': self.operator,
            'payout_type': self.payout_type,
            'interval': self.interval,
            'amount': self.amount
        }

    def to_string(self):
        return (
            ('true' if self.active else 'false') +
            self.get_string(self.operator) +
            self.get_string(self.payout_type) +
            self.get_string(self.interval) +
            self.get_string(self.amount)
        )


class AffiliateContract(Contract):

    def __init__(
        self,
        version,
        expiry,
        contract_type,
        affiliate_proof_type,
        target,
        market,
        identity,
        creator,
        referrer,
        referee
    ):
        super().__init__(
            version,
            expiry,
            contract_type,
            affiliate_proof_type,
            identity,
            creator
        )

        self.affiliate_proof_type = affiliate_proof_type

        self.referrer = referrer if isinstance(referrer, ReferPayout) else ReferPayout(**referrer)

        self.referee = referee if isinstance(referee, ReferPayout) else ReferPayout(**referee)

        self.target = target

        self.market = market

    @classmethod
    async def generate(
        cls,
        expiry=(time.time() + (60 * 60)),
        contract_type=ContractTypes.NEW_RELATIONSHIP.value,
        affiliate_proof_type=AffiliatePoofTypes.HONOR.value,
        target='',
        market='',
        username='',
        creator=None,
        referrer=None,
        referee=None
    ):
        identity = PrivateIdentity.generate(username)
        return cls(
            version=1,
            expiry=expiry,
            contract_type=contract_type,
            affiliate_proof_type=affiliate_proof_type,
            target=target,
            market=market,
            identity=identity,
            creator=creator,
            referrer=referrer,
            referee=referee
        )

    async def process(self, contract_txn, trigger_txn, mempool_txns):
        from yadacoin.core.transaction import Transaction
        from yadacoin.core.transactionutils import TU

        await self.verify(contract_txn, trigger_txn, mempool_txns)

        address = str(P2PKHBitcoinAddress.from_pubkey(bytes.fromhex(self.identity.public_key)))
        value_sent_to_address = sum([x.value for x in trigger_txn.outputs if x.to == address])

        referrer = await self.get_referrer(trigger_txn)
        if not referrer:
            return

        outputs = []
        try:
            await self.verify_payout_generated_already(contract_txn, trigger_txn, self.referrer, mempool_txns)
            if self.referrer.active:
                to = str(P2PKHBitcoinAddress.from_pubkey(bytes.fromhex(referrer.public_key)))
                if self.referrer.operator == PayoutOperators.PERCENT.value:
                    value = self.referrer.amount * value_sent_to_address
                if self.referrer.operator == PayoutOperators.FIXED.value:
                    value = self.referrer.amount

                output = Output(
                    to=to,
                    value=value
                )
                outputs.append(output)
        except:
            pass
        try:
            await self.verify_payout_generated_already(contract_txn, trigger_txn, self.referee, mempool_txns)
            if self.referee.active:
                to = str(P2PKHBitcoinAddress.from_pubkey(bytes.fromhex(trigger_txn.public_key)))
                if self.referee.operator == PayoutOperators.PERCENT.value:
                    value = self.referee.amount * value_sent_to_address
                if self.referee.operator == PayoutOperators.FIXED.value:
                    value = self.referee.amount

                output = Output(
                    to=to,
                    value=value
                )
                outputs.append(output)
        except:
            pass

        if outputs:
            total_payout = sum([x.value for x in outputs])

            payout_txn = await Transaction.generate(
                fee=0,
                outputs=outputs,
                public_key=self.identity.public_key,
                requester_rid=trigger_txn.requester_rid,
                requested_rid=contract_txn.requested_rid,
                rid=trigger_txn.rid,
                private_key=binascii.hexlify(base58.b58decode(self.identity.wif))[2:-10].decode()
            )
            payout_txn.miner_signature = TU.generate_signature_with_private_key(
                self.config.private_key,
                hashlib.sha256(payout_txn.transaction_signature.encode()).hexdigest()
            )
            return payout_txn

    async def verify_honor(self, contract_txn, trigger_txn):
        referrer = await self.get_referrer(trigger_txn)
        if not referrer:
            raise Exception('Referrer not found')

        if trigger_txn.requested_rid != contract_txn.requested_rid:
            raise Exception('Referee is not for this contract')

    async def verify_payout_generated_already(self, contract_txn, trigger_txn, participant, mempool_txns):

        for txn in mempool_txns:
            if (
                txn.public_key == self.identity.public_key and
                txn.requester_rid == trigger_txn.requester_rid and
                txn.requested_rid == contract_txn.requested_rid and
                txn.rid == trigger_txn.rid
            ):
                raise Exception('Contract already generated in this mempool')
        match = {
            'transactions.public_key': self.identity.public_key,
            'transactions.requester_rid': trigger_txn.requester_rid,
            'transactions.requested_rid': contract_txn.requested_rid,
            'transactions.rid': trigger_txn.rid
        }
        block_results = self.config.mongo.async_db.blocks.aggregate([
            {
                '$match': match
            },
            {
                '$unwind': '$transactions'
            },
            {
                '$match': match
            },
            {
                '$sort': {'index': -1, 'transactions.fee': -1, 'transactions.time': -1}
            }
        ])
        async for block_result in block_results:
            if participant.payout_type == PayoutType.RECURRING.value:
                if block_result['index'] > self.config.LatestBlock.block.index - participant.interval:
                    raise Exception('Contract already generated payout for this interval')
            elif participant.payout_type == PayoutType.ONE_TIME.value:
                if block_result:
                    raise Exception('Contract already generated payout')
            break

    async def get_honor_funds(self, contract_txn, total_payout):
        address = str(P2PKHBitcoinAddress.from_pubkey(bytes.fromhex(self.identity.public_key)))
        funds = self.get_funds(contract_txn)
        total_output_to_contract = 0
        selected_funds = []
        async for fund in funds:
            total_output_to_contract += sum([x.value for x in fund.outputs if x.to == address])
            selected_funds.append(fund)

            if total_output_to_contract >= total_payout:
                break
        return selected_funds

    async def get_referrer(self, trigger_txn):
        from yadacoin.core.transaction import Transaction
        referrers = self.config.mongo.async_db.blocks.aggregate([
            {
                '$match': {
                    'transactions.rid': trigger_txn.requester_rid
                }
            },
            {
                '$unwind': '$transactions'
            },
            {
                '$match': {
                    'transactions.rid': trigger_txn.requester_rid
                }
            },
            {
                '$sort': {'index': 1, 'transactions.time': 1}
            },
            {
                '$limit': 1
            }
        ])
        results = [referrer async for referrer in referrers]
        if results:
            return Transaction.from_dict(results[0]['transactions'])

    def to_dict(self):
        return {
            Collections.SMART_CONTRACT.value: {
                'version': self.version,
                'expiry': self.expiry,
                'contract_type': self.contract_type,
                'affiliate_proof_type': self.affiliate_proof_type,
                'target': self.target,
                'market': self.market,
                'identity': self.identity.to_dict,
                'referrer': self.referrer.to_dict(),
                'referee': self.referee.to_dict(),
                'creator': self.creator
            }
        }

    def to_string(self):
        return (
            self.get_string(self.version) +
            self.get_string(self.expiry) +
            self.get_string(self.contract_type) +
            self.get_string(self.affiliate_proof_type) +
            self.get_string(self.target) +
            self.get_string(self.market) +
            self.get_string(self.identity.username_signature) +
            self.referrer.to_string() +
            self.referee.to_string() +
            self.get_string(self.creator)
        )