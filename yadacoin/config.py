import binascii
import hashlib
import json
from logging import getLogger
from time import time

import base58
from bip32utils import BIP32Key
from bitcoin.wallet import P2PKHBitcoinAddress
from coincurve import PrivateKey, PublicKey
from mnemonic import Mnemonic

CONFIG = None


def get_config():
    return CONFIG


class Config(object):

    def __init__(self, config):
        self.start_time = int(time())
        self.seed = config.get('seed', '')
        self.xprv = config.get('xprv', '')
        self.username = config.get('username', '')
        self.network = config.get('network', 'mainnet')
        self.use_pnp = config.get('use_pnp', True)
        self.ssl = config.get('ssl', False)
        self.origin = config.get('origin', False)
        self.max_inbound = config.get('max_inbound', 10)
        self.max_outbound = config.get('max_outbound', 10)
        self.max_miners = config.get('max_miners', -1)
        self.pool_payout = config.get('pool_payout', False)
        self.polling = config.get('polling', 30)
        if 0 < self.polling < 30:
            getLogger("tornado.application").error("Using too small a polling value ({}), use 0 or > 30"
                                                   .format(self.polling))
        self.public_key = config['public_key']
        self.address = str(P2PKHBitcoinAddress.from_pubkey(bytes.fromhex(self.public_key)))

        self.private_key = config['private_key']
        self.wif = self.to_wif(self.private_key)
        self.bulletin_secret = self.get_bulletin_secret()

        self.mongodb_host = config['mongodb_host']
        self.database = config['database']
        self.site_database = config['site_database']
        self.web_server_host = config['web_server_host']
        self.web_server_port = config['web_server_port']
        if config['peer_host'] == '0.0.0.0' or config['peer_host'] == 'localhost':
            raise Exception("Cannot use localhost or 0.0.0.0, must specify public ipv4 address")
        if config['peer_host'] == '[my public ip]':
            raise Exception("Please configure your peer_post to your public ipv4 address")
        self.peer_host = config['peer_host']
        self.peer_port = config['peer_port']
        self.serve_host = config['serve_host']
        self.serve_port = config['serve_port']
        self.callbackurl = config['callbackurl']
        self.sia_api_key = config.get('sia_api_key')
        self.jwt_public_key = config.get('jwt_public_key')
        self.fcm_key = config['fcm_key']
        self.post_peer = config.get('post_peer', True)
        self.extended_status = config.get('extended_status', False)
        self.peers_seed = config.get('peers_seed', [])  # not used, superceeded by config/seed.json
        self.api_whitelist = config.get('api_whitelist', [])
        self.force_broadcast_to = config.get('force_broadcast_to', [])
        self.force_polling = config.get('force_polling', [])
        self.outgoing_blacklist =  config.get('outgoing_blacklist', [])
        # Do not try to test or connect to ourselves.
        self.outgoing_blacklist.append(self.serve_host)
        self.outgoing_blacklist.append("{}:{}".format(self.peer_host, self.peer_port))
        self.protocol_version = 1
        # Config also serves as backbone storage for all singleton helpers used by the components.
        self.mongo = None
        self.consensus = None
        self.peers = None
        self.BU = None
        self.GU = None
        self.SIO = None
        self.debug = False
        self.mp = None
        self.pp = None
        self.stratum_pool_port = config.get('stratum_pool_port', 3333)
        self.wallet_host_port = config.get('wallet_host_port', 'http://localhost:{}'.format(config['peer_port']))

    async def on_new_block(self, block):
        """Dispatcher for the new bloc event
        This is called with a block object when we insert a new one in the chain."""
        # Update BU
        # We can either invalidate, or directly set the block as cached one.
        # self.BU.invalidate_last_block()
        block_dict = block.to_dict()
        self.BU.set_latest_block(block_dict)  # Warning, this is a dict, not a Block!
        await self.mp.refresh()

    def debug_log(self, string: str):
        # Helper to write temp string to a debug file
        with open("debug.log", "a") as fp:
            fp.write(str(int(time())) + ' - ' + string + "\n")

    def get_status(self):
        pool_status = 'N/A'
        if self.mp:
            pool_status = self.mp.get_status()
        m, s = divmod(int(time() - self.start_time), 60)
        h, m = divmod(m, 60)
        status = {'version': self.protocol_version, 'network': self.network,
                  # 'connections':{'outgoing': -1, 'ingoing': -1, 'max': -1},
                  'peers': self.peers.get_status(),
                  'pool': pool_status, 'height': self.BU.get_latest_block()['index'],
                  'uptime': '{:d}:{:02d}:{:02d}'.format(h, m, s)}
        # TODO: add uptime in human readable format
        return status

    @classmethod
    def generate(cls, xprv=None, prv=None, seed=None, child=None, username=None, mongodb_host=None):
        from miniupnpc import UPnP
        mnemonic = Mnemonic('english')
        # generate 12 word mnemonic seed
        if not seed and not xprv and not prv:
            seed = mnemonic.generate(256)
        private_key = None
        if seed:
            # create new wallet
            entropy = mnemonic.to_entropy(seed)
            key = BIP32Key.fromEntropy(entropy)
            private_key = key.PrivateKey().hex()
            extended_key = key.ExtendedKey()
            public_key = PublicKey.from_point(key.K.pubkey.point.x(), key.K.pubkey.point.y()).format().hex()
            address = str(key.Address())

        if prv:
            key = PrivateKey.from_hex(prv)
            private_key = key.to_hex()
            extended_key = ''
            public_key = key.public_key.format().hex()
            address = str(P2PKHBitcoinAddress.from_pubkey(bytes.fromhex(public_key)))

        if xprv:
            key = BIP32Key.fromExtendedKey(xprv)
            private_key = key.PrivateKey().hex()
            extended_key = key.ExtendedKey()
            public_key = PublicKey.from_point(key.K.pubkey.point.x(), key.K.pubkey.point.y()).format().hex()
            address = str(key.Address())
        
        if xprv and child:
            for x in child:
                key = key.ChildKey(int(x))
                private_key = key.PrivateKey().hex()
                public_key = PublicKey.from_point(key.K.pubkey.point.x(), key.K.pubkey.point.y()).format().hex()
                address = str(key.Address())

        if not private_key:
            raise Exception('No key')
        
        try:
            u = UPnP(None, None, 200, 0)
            u.discover()
            u.selectigd()
            peer_host = u.externalipaddress()
        except:
            try:
                import urllib.request
                peer_host = urllib.request.urlopen('https://ident.me').read().decode('utf8')
            except:
                peer_host = ''

        return cls({
            "seed": seed or '',
            "xprv": extended_key or '',
            "private_key": private_key,
            "wif": cls.generate_wif(private_key),
            "public_key": public_key,
            "address": address,
            "api_whitelist": [],
            "serve_host": "0.0.0.0",
            "serve_port": 8000,
            "use_pnp": True,
            "ssl": False,
            "origin": '',
            "polling": 30,
            "sia_api_key": '',
            "post_peer": False,
            "peer_host": peer_host,
            "peer_port": 8000,
            "web_server_host": "0.0.0.0",
            "web_server_port": 8000,
            "peer": "http://localhost:8000",
            "callbackurl": "http://0.0.0.0:8000/create-relationship",
            "jwt_public_key": None,
            "fcm_key": "",
            "database": "yadacoin",
            "site_database": "yadacoinsite",
            "mongodb_host": mongodb_host or "localhost",
            "mixpanel": "",
            "username": username or '',
            "network": "mainnet",
            "wallet_host_port": 'http://localhost:8000',
        })

    @classmethod
    def from_dict(cls, config):
        from yadacoin.transactionutils import TU

        cls.seed = config.get('seed', '')
        cls.xprv = config.get('xprv', '')
        cls.username = config.get('username', '')
        cls.use_pnp = config.get('use_pnp', True)
        cls.ssl = config.get('ssl', True)
        cls.origin = config.get('origin', True)
        cls.polling = config.get('polling', -1)
        cls.network = config.get('network', 'mainnet')
        cls.public_key = config['public_key']
        cls.address = str(P2PKHBitcoinAddress.from_pubkey(bytes.fromhex(cls.public_key)))

        cls.pool_payout = config.get('pool_payout', False)
        cls.private_key = config['private_key']
        cls.wif = cls.generate_wif(cls.private_key)
        cls.bulletin_secret = TU.generate_deterministic_signature(config, config['username'], config['private_key'])

        cls.api_whitelist = config.get('api_whitelist', [])
        cls.mongodb_host = config['mongodb_host']
        cls.database = config['database']
        cls.site_database = config['site_database']
        cls.web_server_host = config['web_server_host']
        cls.web_server_port = config['web_server_port']
        if config['peer_host'] == '0.0.0.0' or config['peer_host'] == 'localhost':
            raise Exception("cannot use localhost or 0.0.0.0, must specify public ipv4 address")
        if config['peer_host'] == '[my public ip]':
            raise Exception("please configure your peer_post to your public ipv4 address")
        cls.peer_host = config['peer_host']
        cls.peer_port = config['peer_port']
        cls.serve_host = config['serve_host']
        cls.serve_port = config['serve_port']
        cls.callbackurl = config['callbackurl']
        cls.fcm_key = config['fcm_key']
        cls.jwt_public_key = config.get('jwt_public_key')
        cls.sia_api_key = config.get('sia_api_key')
        cls.wallet_host_port = config.get('wallet_host_port')

    def get_bulletin_secret(self):
        from yadacoin.transactionutils import TU
        return TU.generate_deterministic_signature(self, self.username, self.private_key)

    def to_wif(self, private_key):
        private_key_static = private_key
        extended_key = "80"+private_key_static+"01"
        first_sha256 = hashlib.sha256(binascii.unhexlify(extended_key)).hexdigest()
        second_sha256 = hashlib.sha256(binascii.unhexlify(first_sha256)).hexdigest()
        final_key = extended_key+second_sha256[:8]
        wif = base58.b58encode(binascii.unhexlify(final_key)).decode('utf-8')
        return wif

    @classmethod
    def generate_wif(cls, private_key):
        private_key_static = private_key
        extended_key = "80"+private_key_static+"01"
        first_sha256 = hashlib.sha256(binascii.unhexlify(extended_key)).hexdigest()
        second_sha256 = hashlib.sha256(binascii.unhexlify(first_sha256)).hexdigest()
        final_key = extended_key+second_sha256[:8]
        wif = base58.b58encode(binascii.unhexlify(final_key)).decode('utf-8')
        return wif

    def to_dict(self):
        return {
            'seed': self.seed,
            'xprv': self.xprv,
            'public_key': self.public_key,
            'address': self.address,
            'private_key': self.private_key,
            'wif': self.wif,
            'bulletin_secret': self.bulletin_secret,
            'mongodb_host': self.mongodb_host,
            'api_whitelist': self.api_whitelist,
            'username': self.username,
            'network': self.network,
            'database': self.database,
            'site_database': self.site_database,
            'web_server_host': self.web_server_host,
            'web_server_port': self.web_server_port,
            'peer_host': self.peer_host,
            'peer_port': self.peer_port,
            'serve_host': self.serve_host,
            'serve_port': self.serve_port,
            'use_pnp': self.use_pnp,
            'ssl': self.ssl,
            'origin': self.origin,
            'fcm_key': self.fcm_key,
            'polling': self.polling,
            'sia_api_key': self.sia_api_key,
            'jwt_public_key': self.jwt_public_key,
            'callbackurl': self.callbackurl,
            'wallet_host_port': self.wallet_host_port,
        }

    def to_json(self):
        return json.dumps(self.to_dict(), indent=4)
