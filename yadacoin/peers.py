import json
from time import time
from random import choice
from tornado import ioloop
from tornado.httpclient import AsyncHTTPClient, HTTPRequest
from tornado.httputil import HTTPHeaders
from asyncio import sleep as async_sleep, gather
from pymongo import ASCENDING, DESCENDING
from logging import getLogger

from yadacoin.core.config import get_config


class Peers(object):
    """A Peer manager Class."""

    peers = []
    # peers_json = ''

    def __init__(self):
        self.config = get_config()
        self.mongo = self.config.mongo
        self.network = self.config.network
        self.my_peer = None
        self.app_log = getLogger("tornado.application")
        self.inbound = {}  # a dict of inbound streams, keys are sids
        self.outbound = {}  # a dict of outbound streams, keys are ips
        self.connected_ips = []  # a list of peers ip we're connected to
        # I chose to have 2 indexs and more memory footprint rather than iterating over one to get the other.
        self.probable_old_nodes = {}  # dict : keys are ip, value time when to delete from list
        self.syncing = False
        my_peer = self.mongo.db.config.find_one({'mypeer': {"$ne": ""}})
        if my_peer:
            self.my_peer = my_peer.get('mypeer')  # str
        self.app_log.debug(self.my_peer)

    def init_local(self):
        raise RuntimeError("Peers, init_local is deprecated")
        return '[]'

        self.my_peer = self.mongo.db.config.find_one({'mypeer': {"$ne": ""}}).get('mypeer')
        res = self.mongo.db.peers.find({'active': True, 'failed': {'$lt': 300}}, {'_id': 0})
        try:
            # Do not include ourselve in the list
            added_hosts = []
            self.peers = []
            for peer in res:
                if peer['host'] in self.config.outgoing_blacklist:
                    continue

                if peer['host'] in added_hosts:
                    continue

                self.peers.append(Peer(peer['host'], peer['port']))
                added_hosts.append(peer['host'])
        except:
            pass
        return self.to_json()

    def get_status(self):
        """Returns peers status as explicit dict"""
        # TODO: cache?
        status = {"inbound": len(self.inbound), "outbound": len(self.outbound)}
        if self.config.extended_status:
            # print(self.inbound)
            status['inbound_detail'] = [peer['ip'] for sid, peer in self.inbound.items()]
            status['outbound_detail'] = list(self.outbound.keys())
            status['probable_old_nodes'] = self.probable_old_nodes
            status['connected_ips'] = self.connected_ips
            # TODO: too many conversions from/to object and string
            status['peers'] = [peer.to_string() for peer in self.peers]
        return status

    @property
    def free_inbound_slots(self):
        """How many free inbound slots we have"""
        return self.config.max_inbound - len(self.inbound)

    @property
    def free_outbound_slots(self):
        """How many free outbound slots we have"""
        return self.config.max_outbound - len(self.outbound)

    def potential_outbound_peers(self):
        """List the working peers we know, we are not yet connected to"""
        now = time()
        # remove after timeout
        self.probable_old_nodes = {key: delete_at
                                   for key, delete_at in self.probable_old_nodes.items()
                                   if delete_at > now}
        return [peer for peer in self.peers
                if peer.host not in self.connected_ips
                and peer.host not in self.probable_old_nodes
                and peer.host not in self.config.outgoing_blacklist
                and not (peer.host == self.config.peer_host and peer.port == self.config.peer_port)]

    def allow_ip(self, IP):
        """Returns True if that ip can connect - inbound or outbound"""
        # TODO - add blacklist
        # TODO: if verbose, say why
        print(self.connected_ips)
        return True  # Allowing all since multiple nodes can run under the same IP

    def on_new_ip(self, ip):
        """We got an inbound or initiate an outbound connection from/to an ip, but do not have the result yet.
        avoid initiating one connection twice if the handshake does not go fast enough."""
        self.app_log.info("on_new_ip:{}".format(ip))
        if ip not in self.connected_ips:
            self.connected_ips.append(ip)

    def on_lost_ip(self, ip):
        """Remove an ip that was not registered as outgoing or ingoing yet"""
        self.app_log.info("on_lost_ip:{}".format(ip))
        self.connected_ips.remove(ip)

    async def on_new_inbound(self, ip:str, port:int, version, sid):
        """Inbound peer provided a correct version and ip, add it to our pool"""
        self.app_log.info("on_new_inbound {}:{} {}".format(ip, port, version))
        if ip not in self.connected_ips:
            self.connected_ips.append(ip)
        # TODO: maybe version is not to be stored, then we could only store ip:port as string to avoid dict overhead.
        self.inbound[sid] = {"ip":ip, "port":port, "version": version}
        # maybe it's an ip we don't have yet, add it
        await self.on_new_peer_list([{'host': ip, 'port': port}])

    async def on_close_inbound(self, sid, ip=''):
        # If the peer was fully connected, then it'in inbound.
        # If not, we have no full info, but an ip optional field.
        self.app_log.info("on_close_inbound {} - ip {}".format(sid, ip))
        info = self.inbound.pop(sid, None)
        try:
            stored_ip = info['ip']
            self.connected_ips.remove(stored_ip)
        except:
            pass
        if ip:
            self.connected_ips.remove(ip)

    def on_new_outbound(self, ip, port, client):
        """Outbound peer connection was successful, add it to our pool"""
        self.app_log.info("on_new_outbound {}:{}".format(ip, port))
        if ip not in self.connected_ips:
            self.connected_ips.append(ip)
        self.outbound[ip] = {"ip":ip, "port":port, "client": client}

    def on_close_outbound(self, ip):
        # We only allow one in or out per ip
        self.app_log.info("on_close_outbound {}".format(ip))
        self.outbound.pop(ip, None)
        self.connected_ips.remove(ip)

    async def check_outgoing(self):
        """Called by a background task.
        Counts the current active outgoing connections, and tries to connect to more if needed"""
        if len(self.peers) < 1:
            await self.refresh()
        if self.free_outbound_slots <= 0:
            return
        targets = self.potential_outbound_peers()
        if len(targets) <= 0:
            return
        peer = choice(targets)  # random peer from the available - and tested - pool
        # Try to connect. We create a background co-routine that will handle the client side.
        await self.background_peer(peer)

    async def background_peer(self, peer):
        self.app_log.debug("Peers background_peer {}".format(peer.to_dict()))
        # lock that ip
        self.on_new_ip(peer.host)
        try:
            peer.client = YadaWebSocketClient(peer)
            # This will run until disconnect
            await peer.client.start()
        except Exception as e:
            self.app_log.warning("Error: {} on background_peer {}".format(e, peer.host))
        finally:
            # If we get here with no outbound record, then it was an old node.
            #if peer.host not in self.outbound:
            if peer.client and peer.client.probable_old:
                # add it to a temp "do not try ws soon" list
                self.app_log.debug("Peer {} added to probable_old_nodes".format(peer.host))
                self.probable_old_nodes[peer.host] = int(time()) + 3600  # try again in 1 hour
                await peer.client.client.disconnect()
            self.on_close_outbound(peer.host)

    async def on_block_insert(self, block_data: dict):
        """This is what triggers the event to all connected ws peers, in or outgoing"""
        # outgoing
        self.app_log.debug("Block Insert event index {}".format(block_data['index']))
        # Update the miners (/pool http route) is done via latest_block => move to event to MiningPool stored by config
        if self.config.mp:
            await self.config.mp.refresh(block_data)
            # Update the miners (websockets)
            await self.config.SIO.emit('header', data=await self.config.mp.block_to_mine_info(), namespace='/pool')
        # TODO: start all async at once then await gather to spare some delay
        for ip, outgoing in self.outbound.items():
            try:
                await outgoing['client'].emit("latest_block", data=block_data, namespace="/chat")
            except Exception as e:
                self.app_log.warning("Error {} notifying outgoing {}".format(e, ip))
        # ingoing
        try:
            await self.config.SIO.emit("latest_block", data=block_data, namespace="/chat")
        except Exception as e:
            self.app_log.warning("Error {} notifying ws clients".format(e))

    async def refresh(self):
        """Refresh the in-memory peer list from db and api. Only contains Active peers"""
        self.app_log.info("Async Peers refresh")
        if self.network == 'regnet':
            peer = await self.mongo.async_db.config.find_one({
                # 'mypeer': {"$ne": ""},
                'mypeer': {'$exists': True}
            })
            if not peer:
                return
            # Insert ourself to have at least one peer. Not sure this is required, but allows for more tests coverage.
            self.peers=[Peer(self.config.serve_host, self.config.serve_port,
                             peer.get('bulletin_secret'))]
            return
        url = 'https://yadacoin.io/peers'  # Default value
        if self.network == 'testnet':
            url = 'https://yadacoin.io:444/peers'

        res = await self.mongo.async_db.peers.find({'active': True, 'net':self.network}, {'_id': 0}).to_list(length=100)
        if len(res) <= 0:
            # Our local db gives no match, get from seed list if we did not just now
            last_seeded = await self.mongo.async_db.config.find_one({'last_seeded': {"$exists": True}})
            # print(last_seeded)
            try:
                if last_seeded and int(last_seeded['last_seeded']) + 60 * 10 > time():
                    # 10 min mini between seed requests
                    self.app_log.info('Too soon, waiting for seed...')
                    return
            except Exception as e:
                self.app_log.error("Error: {} last_seeded".format(e))

            test_after = int(time())  # new peers will be tested asap.
            if len(self.config.peers_seed):
                # add from our config file
                await self.on_new_peer_list(self.config.peers_seed, test_after)
            else:
                self.app_log.warning("No seed.json with config?")
                # or from central yadacoin.io if none
                try:
                    response = await self.config.http_client.fetch(url)
                    seeds = json.loads(response.body.decode('utf-8'))['get-peers']
                    if len(seeds['peers']) <= 0:
                        self.app_log.warning("No peers on main yadacoin.io node")
                    await self.on_new_peer_list(seeds['peers'], test_after)
                except Exception as e:
                    self.app_log.warning("Error: {} on url {}".format(e, url))
            await self.mongo.async_db.config.replace_one({"last_seeded": {"$exists": True}}, {"last_seeded": str(test_after)}, upsert=True)
            # self.mongo.db.config.update({'last_seeded': {"$ne": ""}}, {'last_seeded': str(test_after)}, upsert=True)

        # todo: probly more efficient not to rebuild the objects every time
        self.peers = []
        for peer in res:
            if peer['host'] in self.peers:
                continue
            self.peers.append(Peer(peer['host'], peer['port']))
        self.app_log.debug("Peers count {}".format(len(self.peers)))

    async def on_new_peer_list(self, peer_list: list, test_after=None):
        """Process an external peer list, and saves the new ones"""
        if test_after is None:
            test_after = int(time())  # new peers will be tested asap.
        already_used = []
        for peer in peer_list:
            if peer['host'] in already_used or peer['host'] == self.config.peer_host:
                continue
            already_used.append(peer['host'])
            res = await self.mongo.async_db.peers.count_documents({'host': peer['host'], 'port': peer['port']})
            if res > 0:
                # We know him already, so it will be tested.
               self.app_log.debug('Known peer {}:{}'.format(peer['host'], peer['port']))
            else:
                await self.mongo.async_db.peers.insert_one({
                    'host': peer['host'], 'port': peer['port'], 'net': self.network,
                    'active': False, 'failed': 0, 'test_after': test_after})
                # print('Inserted')
                self.app_log.debug("Inserted new peer {}:{}".format(peer['host'], peer['port']))

    async def test_some(self, count=1):
        """Tests count peers from our base, by priority"""
        try:
            res = self.mongo.async_db.peers.find({'active': False, 'net': self.network, 'test_after': {"$lte": int(time())}}).sort('test_after', ASCENDING).limit(count)
            to_test = []
            async for a_peer in res:
                peer = Peer(a_peer['host'], a_peer['port'])
                # print("Testing", peer)
                to_test.append(peer.test())
            res = await gather(*to_test)
            # print('res', res)
        except Exception as e:
            self.app_log.warning("Error: {} on test_some".format(e))
        # to_list(length=100)

    async def increment_failed(self, peer):
        if peer.host in [x['host'] for x in self.config.peers_seed]:
            return
        res = await self.mongo.async_db.peers.find_one({'host': peer.host, 'port': int(peer.port)})
        if not res:
            res = {}
        failed = res.get('failed', 0) + 1
        factor = failed
        if failed > 20:
            factor = 240  # at most, test every 4 hours
        elif failed > 10:
            factor = 6 * factor
        elif failed > 5:
            factor = 2 * factor
        test_after = int(time()) + factor * 60  #
        await self.mongo.async_db.peers.update_one({'host': peer.host, 'port': int(peer.port)},
                                                   {'$set': {'active': False, "test_after": test_after,
                                                             "failed": failed}}, upsert=True)
        # remove from in memory list
        self.peers = [apeer for apeer in self.peers if apeer.host != peer.host]

    @classmethod
    def from_dict(cls):
        raise RuntimeError("Peers, from_dict is deprecated")
        """
        cls.peers = []
        for peer in config['peers']:
            cls.peers.append(
                Peer(
                    config,
                    mongo,
                    peer['host'],
                    peer['port'],
                    peer.get('bulletin_secret')
                )
            )
        """

    def to_dict(self):
        peers = [x.to_dict() for x in self.peers]
        return {
            'num_peers': len(peers),
            'peers': peers
        }

    def to_json(self):
        return json.dumps(self.to_dict(), indent=4)
