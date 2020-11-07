import json
import hashlib
import time
from collections import OrderedDict
from logging import getLogger

from yadacoin.core.config import get_config
from yadacoin.core.identity import Identity
from yadacoin.core.latestblock import LatestBlock
from yadacoin.core.transaction import Transaction


class Peer:
    id_attribute = 'rid'
    """An individual Peer object"""
    epoch = 1602914018
    ttl = 259200

    def __init__(self, host=None, port=None, identity=None, seed=None, seed_gateway=None):
        self.host = host
        self.port = port
        self.identity = identity
        self.seed = seed
        self.seed_gateway = seed_gateway
        self.config = get_config()
        self.app_log = getLogger("tornado.application")
    
    @classmethod
    def from_dict(cls, peer, is_me=False):
        inst = cls(
            peer['host'],
            peer['port'],
            Identity.from_dict(peer['identity']),
            seed=peer.get('seed'),
            seed_gateway=peer.get('seed_gateway')
        )
        return inst
    
    @property
    def rid(self):
        if hasattr(self.config, 'peer'):
            return self.identity.generate_rid(self.config.peer.identity.username_signature)

    @classmethod
    def create_upnp_mapping(cls, config):
        from miniupnpc import UPnP
        config = get_config()
        try:
            u = UPnP(None, None, 200, 0)
            u.discover()
            config.igd = u.selectigd()
        except:
            config.igd = ""
        if config.use_pnp:
            import socket
            # deploy as an eventlet WSGI server
            try:
                server_port = config.peer_port
                eport = server_port
                r = u.getspecificportmapping(eport, 'TCP')
                if r:
                    u.deleteportmapping(eport, 'TCP')
                u.addportmapping(eport, 'TCP', u.lanaddr, server_port, 'UPnP YadaCoin Serve port %u' % eport, '')
                config.serve_host = '0.0.0.0'
                config.serve_port = server_port
                config.peer_host = u.externalipaddress()
                config.peer_port = server_port
            except Exception as e:
                print(e)
                config.serve_host = config.serve_host
                config.serve_port = config.serve_port
                config.peer_host = config.peer_host
                config.peer_port = config.peer_port
                print('UPnP failed: you must forward and/or whitelist port', config.peer_port)

    @classmethod
    def type_limit(cls, peer):
        raise NotImplementedError()

    async def get_outbound_class(self):
        raise NotImplementedError()

    async def get_inbound_class(self):
        raise NotImplementedError()

    async def get_outbound_peers(self):
        raise NotImplementedError()

    async def calculate_seed_gateway(self, nonce=None):
        if self.__class__ not in [Group, ServiceProvider]:
            raise Exception('Should not calculate a seed gateway for anything other than groups or service providers')
        username_signature_hash = hashlib.sha256(self.identity.username_signature.encode()).hexdigest()
        # TODO: introduce some kind of unpredictability here. This uses the latest block hash. 
        # So we won't be able to get the new seed without the block hash
        # which is not known in advance
        seed_time = int((time.time() - self.epoch) / self.ttl) + 1
        seed_select = (int(username_signature_hash, 16) * seed_time) % len(self.config.seed_gateways)
        username_signatures = list(self.config.seed_gateways)
        first_number = seed_select
        num_reset = False
        while self.config.seed_gateways[username_signatures[seed_select]].rid in self.config.nodeClient.outbound_ignore[SeedGateway.__name__]:
            seed_select += 1
            if num_reset and seed_select >= first_number:
                break # failed to find a seed gateway
            if seed_select >= len(self.config.seed_gateways) + 1:
                if first_number > 0:
                    seed_select = 0
                

        seed_gateway = self.config.seed_gateways[list(self.config.seed_gateways)[seed_select]]
        return seed_gateway
    
    async def ensure_peers_connected(self):
        peers = await self.get_outbound_peers()
        outbound_class = await self.get_outbound_class()
        limit = self.__class__.type_limit(outbound_class)
        stream_collection = {**self.config.nodeClient.outbound_streams[outbound_class.__name__], **self.config.nodeClient.outbound_pending[outbound_class.__name__]}
        await self.connect(stream_collection, limit, peers)

    async def connect(self, stream_collection, limit, peers):
        if limit and len(stream_collection) < limit:
            for peer in set(peers) - set(stream_collection): # only connect to seed nodes
                await self.config.nodeClient.connect(peers[peer])

    def to_dict(self):
        return {
            'host': self.host,
            'port': self.port,
            'identity': self.identity.to_dict,
            'rid': self.rid,
            'seed': self.seed,
            'seed_gateway': self.seed_gateway
        }

    def to_string(self):
        return '{}:{}'.format(self.host, self.port)
    
    def to_json(self):
        return json.dumps(self.to_dict(), indent=4)
    
    async def get_payload_txn(self, payload):
        txn = None
        if payload.get('transaction'):
            txn = Transaction.from_dict(LatestBlock.block.index, payload.get('transaction'))
        return txn


class Seed(Peer):
    id_attribute = 'rid'
    source_property = 'source_seed'
    async def get_outbound_class(self):
        return Seed

    async def get_inbound_class(self):
        return SeedGateway

    async def get_outbound_peers(self):
        return self.config.seeds

    @classmethod
    def type_limit(cls, peer):
        if peer == Seed:
            return 100000
        elif peer == SeedGateway:
            return 1
        else:
            return 0

    @classmethod
    def compatible_types(cls):
        return [Seed, SeedGateway]
    
    async def get_route_peers(self, peer, payload):
        if isinstance(peer, SeedGateway):
            # this if statement allow bi-directional communication cross-seed
            if self.source_property in payload:
                # this is a response
                bridge_seed = self.config.seeds[payload[self.source_property]]
            else:
                # this must be the identity of the destination service provider
                # the message originator must provide the necissary service provider identity information
                # typically, the originator will grab all mutual service providers of the originator and the recipient of the message
                # and send "through" every service provider so the recipient will receive the message on all services

                peer = Peer.from_dict(payload.get('dest_service_provider'))
                bridge_seed_gateway = await peer.calculate_seed_gateway() # get the seed gateway
                bridge_seed = bridge_seed_gateway.seed
                payload[self.source_property] = self.config.peer.identity.username_signature
            if bridge_seed.rid in self.config.nodeServer.inbound_streams[Seed.__name__]:
                peer_stream = self.config.nodeServer.inbound_streams[Seed.__name__][bridge_seed.rid]
            elif bridge_seed.rid in self.config.nodeClient.outbound_streams[Seed.__name__]:
                peer_stream = self.config.nodeClient.outbound_streams[Seed.__name__][bridge_seed.rid]
            else:
                self.config.app_log.error('No bridge seed found. Cannot route transaction.')
            yield peer_stream
        elif isinstance(peer, Seed):
            for rid, peer_stream in self.config.nodeServer.inbound_streams[SeedGateway.__name__].items():
                yield peer_stream
            for rid, peer_stream in self.config.nodeClient.outbound_streams[Seed.__name__].items():
                yield peer_stream
    
    async def get_service_provider_request_peers(self, peer, payload):
        if isinstance(peer, SeedGateway):
            # this if statement allow bi-directional communication cross-seed
            if self.source_property in payload:
                # this is a response
                bridge_seed_from_payload = Peer.from_dict(payload[self.source_property])
                bridge_seed = self.config.seeds[bridge_seed_from_payload.identity.username_signature]
            else:
                # this must be the identity of the destination service provider
                # the message originator must provide the necissary service provider identity information
                # typically, the originator will grab all mutual service providers of the originator and the recipient of the message
                # and send "through" every service provider so the recipient will receive the message on all services

                bridge_seed_gateway = Peer.from_dict(payload.get('seed_gateway'))
                bridge_seed = self.config.seeds[
                    self.config.seed_gateways[bridge_seed_gateway.identity.username_signature].seed
                ]
                payload[self.source_property] = self.config.peer.identity.username_signature
            if bridge_seed.rid in self.config.nodeServer.inbound_streams[Seed.__name__]:
                peer_stream = self.config.nodeServer.inbound_streams[Seed.__name__][bridge_seed.rid]
            elif bridge_seed.rid in self.config.nodeClient.outbound_streams[Seed.__name__]:
                peer_stream = self.config.nodeClient.outbound_streams[Seed.__name__][bridge_seed.rid]
            else:
                self.config.app_log.error('No bridge seed found. Cannot route transaction.')
            yield peer_stream
        elif isinstance(peer, Seed):
            for rid, peer_stream in self.config.nodeServer.inbound_streams[SeedGateway.__name__].items():
                yield peer_stream


class SeedGateway(Peer):
    id_attribute = 'rid'
    source_property = 'source_seed_gateway'
    async def get_outbound_class(self):
        return Seed

    async def get_inbound_class(self):
        return ServiceProvider

    async def get_outbound_peers(self):
        return {self.config.seeds[self.seed].identity.username_signature: self.config.seeds[self.seed]}

    @classmethod
    def type_limit(cls, peer):
        if peer == Seed:
            return 1
        elif peer == ServiceProvider:
            return 1
        else:
            return 0

    @classmethod
    def compatible_types(cls):
        return [Seed, ServiceProvider]
    
    async def get_route_peers(self, peer, payload):
        if isinstance(peer, Seed):
            for rid, peer_stream in self.config.nodeServer.inbound_streams[ServiceProvider.__name__].items():
                yield peer_stream
        elif isinstance(peer, ServiceProvider):
            for rid, peer_stream in self.config.nodeClient.outbound_streams[Seed.__name__].items():
                yield peer_stream
    
    async def get_service_provider_request_peers(self, peer, payload):
        if isinstance(peer, Seed):
            for rid, peer_stream in self.config.nodeServer.inbound_streams[ServiceProvider.__name__].items():
                yield peer_stream
        elif isinstance(peer, ServiceProvider):
            for rid, peer_stream in self.config.nodeClient.outbound_streams[Seed.__name__].items():
                yield peer_stream


class ServiceProvider(Peer):
    id_attribute = 'rid'
    source_property = 'source_service_provider'

    async def get_outbound_class(self):
        return SeedGateway

    async def get_inbound_class(self):
        return User

    async def get_outbound_peers(self, nonce=None):
        seed_gateway = await self.calculate_seed_gateway()
        return {seed_gateway.identity.username_signature: seed_gateway}

    @classmethod
    def type_limit(cls, peer):
        if peer == SeedGateway:
            return 1
        elif peer == User:
            return 1
        else:
            return 0

    @classmethod
    def compatible_types(cls):
        return [ServiceProvider, User]
    
    async def get_route_peers(self, peer, payload):

        if isinstance(peer, User):
            for rid, peer_stream in self.config.nodeClient.outbound_streams[SeedGateway.__name__].items():
                yield peer_stream

            for rid, peer_stream in self.config.websocketServer.inbound_streams.items():
                if peer.identity.username_signature == peer_stream.peer.identity.username_signature:
                    continue
                yield peer_stream
        
        elif isinstance(peer, SeedGateway):
            txn = self.get_payload_txn(payload)
            if txn:
                txn_sum = sum([x.value for x in txn.outputs])

                if not peer and not txn_sum:
                    self.config.app_log.error('Zero sum transaction and no routing information. Cannot route transaction.')
                    return

                from_peer = None
                if payload.get('from_peer'):
                    from_peer = Identity.from_dict(payload.get('from_peer'))

                rid = None
                if txn.requester_rid in self.config.nodeServer.inbound_streams[User.__name__]:
                    rid = txn.requester_rid
                elif txn.requested_rid in self.config.nodeServer.inbound_streams[User.__name__]:
                    rid = txn.requested_rid
                elif from_peer and from_peer in self.config.nodeServer.inbound_streams[User.__name__]:
                    rid = from_peer.rid
                else:
                    self.config.app_log.error('No user found. Cannot route transaction.')

                if txn_sum:
                    self.config.mongo.async_db.miner_transactions.replace_one(
                        {
                            'id': txn.transaction_signature
                        },
                        txn.to_dict()
                    )
                    for peer_rid, peer_stream in self.config.nodeServer.inbound_streams[User.__name__].items():
                        yield peer_stream
                elif rid:
                    yield self.config.nodeServer.inbound_streams[User.__name__][rid]
    
    async def get_service_provider_request_peers(self, peer, payload):
        # check if the calculated service provider for the group is me
        if payload.get('group'):
            group = Group.from_dict(payload.get('group'))

        if isinstance(peer, User):
            for rid, peer_stream in self.config.nodeClient.outbound_streams[SeedGateway.__name__].items():
                yield peer_stream

            for rid, peer_stream in self.config.websocketServer.inbound_streams.items():
                if peer.identity.username_signature == peer_stream.peer.identity.username_signature:
                    continue
                yield peer_stream
        
        elif isinstance(peer, SeedGateway):
            for peer_rid, peer_stream in self.config.nodeServer.inbound_streams[User.__name__].items():
                yield peer_stream

            for peer_rid, peer_stream in self.config.websocketServer.inbound_streams[User.__name__].items():
                yield peer_stream


class Group(Peer):
    id_attribute = 'rid'

    async def get_outbound_class(self):
        return ServiceProvider

    async def get_inbound_class(self):
        return User

    async def get_outbound_peers(self, nonce=None):
        service_provider = await self.calculate_service_provider()
        return {service_provider.identity.username_signature: service_provider}

    @classmethod
    def type_limit(cls, peer):
        if peer == SeedGateway:
            return 1
        elif peer == User:
            return 1
        else:
            return 0

    @classmethod
    def compatible_types(cls):
        return [ServiceProvider, User]


class User(Peer):
    id_attribute = 'rid'
    async def get_outbound_class(self):
        return ServiceProvider

    async def get_inbound_class(self):
        return User

    async def get_outbound_peers(self):
        return self.config.service_providers

    @classmethod
    def type_limit(cls, peer):
        if peer == ServiceProvider:
            return 1
        else:
            return 0

    @classmethod
    def compatible_types(cls):
        return [ServiceProvider]


class Miner(Peer):
    id_attribute = 'address'
    async def get_outbound_class(self):
        return ServiceProvider

    async def get_inbound_class(self):
        return User

    async def get_outbound_peers(self):
        return self.config.service_providers

    @classmethod
    def type_limit(cls, peer):
        if peer == ServiceProvider:
            return 1
        else:
            return 0

    @classmethod
    def compatible_types(cls):
        return [ServiceProvider]


class Peers:
    
    @classmethod
    def get_seeds(cls):
        return OrderedDict({x.identity.username_signature: x for x in [
            Seed.from_dict({
                'host': '71.193.201.21',
                'port': 8000,
                'identity': {
                    "username": "seed_A",
                    "username_signature": "MEUCIQC3slOHQ0AgPSyFeas/mxMrmJuF5+itfpxSFAERAjyr4wIgCBMuSOEJnisJ7//Y019vYhIWCWvzvCnfXZRxfbrt2SM=",
                    "public_key": "0286707b29746a434ead4ab94af2d7758d4ae8aaa12fdad9ab42ce3952a8ef798f"
                },
                "seed_gateway": "MEQCIEvShxHewQt9u/4+WlcjSubCfsjOmvq8bRoU6t/LGmdLAiAQyr5op3AZj58NzRDthvq7bEouwHhEzis5ZYKlE6D0HA=="
            }),
            Seed.from_dict({
                'host': '71.193.201.21',
                'port': 8008,
                'identity': {
                    "username": "seed_B",
                    "username_signature": "MEQCIBn3IO/QP6UerU5u0XqkTdK0iJpA7apayQgxqgT3E29yAiAljkzDzGucZXSKgjklsuDm9HhjZ70VMjpa21eObQIS7A==",
                    "public_key": "03ef7653e994341268b81a33f35dbfa22cbd240b454a0995ecdd8713cd624a7251"
                },
                "seed_gateway": "MEUCIQCGY5xwZgT5v7iNSpO7b6FFQne8h6RzPf1UAQr2yptHGgIgE6UaVTjyHYozwpona00Ydagkb5oCAiyPv008YL9a5hA="
            })
        ]})

    @classmethod
    def get_seed_gateways(cls):
        return OrderedDict({x.identity.username_signature: x for x in [
            SeedGateway.from_dict({
                'host': '71.193.201.21',
                'port': 8002,
                'identity': {
                    "username": "seed_gateway_A",
                    "username_signature": "MEQCIEvShxHewQt9u/4+WlcjSubCfsjOmvq8bRoU6t/LGmdLAiAQyr5op3AZj58NzRDthvq7bEouwHhEzis5ZYKlE6D0HA==",
                    "public_key": "03e8b4651a1e794998c265545facbab520131cdddaea3da304a36279b1d334dfb1"
                },
                "seed": "MEUCIQC3slOHQ0AgPSyFeas/mxMrmJuF5+itfpxSFAERAjyr4wIgCBMuSOEJnisJ7//Y019vYhIWCWvzvCnfXZRxfbrt2SM="
            }),
            SeedGateway.from_dict({
                'host': '71.193.201.21',
                'port': 8010,
                'identity': {
                    "username": "seed_gateway_B",
                    "username_signature": "MEUCIQCGY5xwZgT5v7iNSpO7b6FFQne8h6RzPf1UAQr2yptHGgIgE6UaVTjyHYozwpona00Ydagkb5oCAiyPv008YL9a5hA=",
                    "public_key": "0308b55c62b0bdce1a696ff21fd94a044ef882328b520341a65d617e8be6964361"
                },
                "seed": "MEQCIBn3IO/QP6UerU5u0XqkTdK0iJpA7apayQgxqgT3E29yAiAljkzDzGucZXSKgjklsuDm9HhjZ70VMjpa21eObQIS7A=="
            })
        ]})

    @classmethod
    def get_service_providers(cls):
        return OrderedDict({x.identity.username_signature: x for x in [
            ServiceProvider.from_dict({
                'host': '71.193.201.21',
                'port': 8004,
                'identity': {
                    "username": "service_provider_A",
                    "username_signature": "MEUCIQCIzIDpRwBJgU0fjTh6FZhpIrLz/WNTLIZwK2Ifx7HjtQIgfYYOPFy7ypU+KYeYzkCa9OWwbwPIt9Hk0cV8Q6pcXog=",
                    "public_key": "0255110297d7b260a65972cd2c623996e18a6aeb9cc358ac667854af7efba4f0a7"
                }
            }),
            ServiceProvider.from_dict({
                'host': '71.193.201.21',
                'port': 80012,
                'identity': {
                    "username": "service_provider_B",
                    "username_signature": "MEQCIF1jg+YOY3r7vR2pF1mLLdnUo/Va9wAQ2X6d6w9fVgLQAiBUyAmw88iMzK/nQ1AK5ZnJqifgXWCH4bid/dlGOJq8EA==",
                    "public_key": "0341f797e55ca256505594e722e2a8c2ed9484d2de12492e704e1d019cef6cf647"
                }
            })
        ]})
    
    @classmethod
    def get_groups(cls):
        return OrderedDict({x.identity.username_signature: x for x in [
            Group.from_dict({
                'host': None,
                'port': None,
                'identity': {
                    'username':'group',
                    'username_signature':'MEUCIQDIlC+SpeLwUI4fzV1mkEsJCG6HIvBvazHuMMNGuVKi+gIgV8r1cexwDHM3RFGkP9bURi+RmcybaKHUcco1Qu0wvxw=',
                    'public_key':'036f99ba2238167d9726af27168384d5fe00ef96b928427f3b931ed6a695aaabff',
                    'wif':'KydUVG4w2ZSQkg6DAZ4UCEbfZz9Tg4PsjJFnvHwFsfmRkqXAHN8W'
                }
            })
        ]})

