# COPYRIGHT (C) 2020 Nicotine+ Team
# COPYRIGHT (C) 2020 Mathias <mail@mathias.is>
# COPYRIGHT (C) 2016-2017 Michael Labouebe <gfarmerfr@free.fr>
# COPYRIGHT (C) 2016 Mutnick <muhing@yahoo.com>
# COPYRIGHT (C) 2013 eL_vErDe <gandalf@le-vert.net>
# COPYRIGHT (C) 2008-2012 Quinox <quinox@users.sf.net>
# COPYRIGHT (C) 2009 Hedonist <ak@sensi.org>
# COPYRIGHT (C) 2006-2009 Daelstorm <daelstorm@gmail.com>
# COPYRIGHT (C) 2003-2004 Hyriand <hyriand@thegraveyard.org>
# COPYRIGHT (C) 2001-2003 Alexander Kanavin
#
# GNU GENERAL PUBLIC LICENSE
#    Version 3, 29 June 2007
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

"""
This is the actual client code. Actual GUI classes are in the separate modules
"""

import configparser
import os
import queue
import shutil
import threading
import time

from gettext import gettext as _
from socket import socket

from pynicotine import slskmessages
from pynicotine import slskproto
from pynicotine import transfers
from pynicotine.config import Config
from pynicotine.geoip.ip2location import IP2Location
from pynicotine.logfacility import log
from pynicotine.pluginsystem import PluginHandler
from pynicotine.shares import Shares
from pynicotine.slskmessages import new_id
from pynicotine.utils import clean_file
from pynicotine.utils import unescape


class PeerConnection:
    """
    Holds information about a peer connection. Not every field may be set
    to something. addr is (ip, port) address, conn is a socket object, msgs is
    a list of outgoing pending messages, token is a reverse-handshake
    number (protocol feature), init is a PeerInit protocol message. (read
    slskmessages docstrings for explanation of these)
    """

    __slots__ = "addr", "username", "conn", "msgs", "token", "init", "type", "conntimer", "tryaddr"

    def __init__(self, addr=None, username=None, conn=None, msgs=None, token=None, init=None, conntimer=None, tryaddr=None):
        self.addr = addr
        self.username = username
        self.conn = conn
        self.msgs = msgs
        self.token = token
        self.init = init
        self.type = init.type
        self.conntimer = conntimer
        self.tryaddr = tryaddr


class Timeout:

    __slots__ = "callback"

    def __init__(self, callback):
        self.callback = callback

    def timeout(self):
        try:
            self.callback([self])
        except Exception as e:
            log.add_warning(_("Exception in callback %s: %s"), (self.callback, e))


class ConnectToPeerTimeout(Timeout):

    __slots__ = "conn"

    def __init__(self, conn, callback):
        self.conn = conn
        self.callback = callback


class NetworkEventProcessor:
    """ This class contains handlers for various messages from the networking thread """

    def __init__(self, ui_callback, network_callback, setstatus, bindip, port, data_dir, config, plugins):

        self.ui_callback = ui_callback
        self.network_callback = network_callback
        self.set_status = setstatus
        self.manualdisconnect = False

        try:
            self.config = Config(config, data_dir)
        except configparser.Error:
            corruptfile = ".".join([config, clean_file(time.strftime("%Y-%m-%d_%H_%M_%S")), "corrupt"])
            shutil.move(config, corruptfile)
            short = _("Your config file is corrupt")
            long = _("We're sorry, but it seems your configuration file is corrupt. Please reconfigure Nicotine+.\n\nWe renamed your old configuration file to\n%(corrupt)s\nIf you open this file with a text editor you might be able to rescue some of your settings."), {'corrupt': corruptfile}
            self.config = Config(config, data_dir)
            self.network_callback([slskmessages.PopupMessage(short, long)])

        # These strings are accessed frequently. We store them to prevent requesting the translation every time.
        self.conn_close_template = _("Connection closed by peer: %s")
        self.conn_remove_template = _("Removed connection closed by peer: %(conn_obj)s %(address)s")

        self.bindip = bindip
        self.port = port

        self.config.read_config()
        log.set_log_levels(self.config.sections["logging"]["debugmodes"])

        self.peerconns = []
        self.watchedusers = []
        self.ipblock_requested = {}
        self.ipignore_requested = {}
        self.ip_requested = []
        self.private_message_queue = {}
        self.users = {}
        self.user_addr_requested = set()
        self.queue = queue.Queue(0)
        self.shares = Shares(self, self.config, self.queue, self.ui_callback)
        self.pluginhandler = PluginHandler(self.ui_callback, plugins, self.config)

        script_dir = os.path.dirname(__file__)
        file_path = os.path.join(script_dir, "geoip/ipcountrydb.bin")
        self.geoip = IP2Location(file_path, "SHARED_MEMORY")

        # Give the logger information about log folder
        self.update_debug_log_options()

        self.protothread = slskproto.SlskProtoThread(self.network_callback, self.queue, self.bindip, self.port, self.config, self)

        uselimit = self.config.sections["transfers"]["uselimit"]
        uploadlimit = self.config.sections["transfers"]["uploadlimit"]
        limitby = self.config.sections["transfers"]["limitby"]

        self.queue.put(slskmessages.SetUploadLimit(uselimit, uploadlimit, limitby))
        self.queue.put(slskmessages.SetDownloadLimit(self.config.sections["transfers"]["downloadlimit"]))

        if self.config.sections["transfers"]["geoblock"]:
            panic = self.config.sections["transfers"]["geopanic"]
            cc = self.config.sections["transfers"]["geoblockcc"]
            self.queue.put(slskmessages.SetGeoBlock([panic, cc]))
        else:
            self.queue.put(slskmessages.SetGeoBlock(None))

        self.active_server_conn = None
        self.waitport = None
        self.chatrooms = None
        self.privatechat = None
        self.globallist = None
        self.userinfo = None
        self.userbrowse = None
        self.search = None
        self.transfers = None
        self.userlist = None
        self.ipaddress = None
        self.privileges_left = None
        self.servertimer = None
        self.server_timeout_value = -1

        self.has_parent = False

        self.requested_info = {}
        self.requested_folders = {}
        self.speed = 0

        # Callback handlers for messages
        self.events = {
            slskmessages.ConnectToServer: self.connect_to_server,
            slskmessages.ConnectError: self.connect_error,
            slskmessages.IncPort: self.inc_port,
            slskmessages.ServerConn: self.server_conn,
            slskmessages.ConnClose: self.conn_close,
            slskmessages.Login: self.login,
            slskmessages.ChangePassword: self.change_password,
            slskmessages.MessageUser: self.message_user,
            slskmessages.PMessageUser: self.p_message_user,
            slskmessages.ExactFileSearch: self.dummy_message,
            slskmessages.UserJoinedRoom: self.user_joined_room,
            slskmessages.SayChatroom: self.say_chat_room,
            slskmessages.JoinRoom: self.join_room,
            slskmessages.UserLeftRoom: self.user_left_room,
            slskmessages.QueuedDownloads: self.dummy_message,
            slskmessages.GetPeerAddress: self.get_peer_address,
            slskmessages.OutConn: self.out_conn,
            slskmessages.UserInfoReply: self.user_info_reply,
            slskmessages.UserInfoRequest: self.user_info_request,
            slskmessages.PierceFireWall: self.pierce_fire_wall,
            slskmessages.CantConnectToPeer: self.cant_connect_to_peer,
            slskmessages.PeerTransfer: self.peer_transfer,
            slskmessages.SharedFileList: self.shared_file_list,
            slskmessages.GetSharedFileList: self.get_shared_file_list,
            slskmessages.FileSearchRequest: self.file_search_request,
            slskmessages.FileSearchResult: self.file_search_result,
            slskmessages.ConnectToPeer: self.connect_to_peer,
            slskmessages.GetUserStatus: self.get_user_status,
            slskmessages.GetUserStats: self.get_user_stats,
            slskmessages.Relogged: self.relogged,
            slskmessages.PeerInit: self.peer_init,
            slskmessages.DownloadFile: self.file_download,
            slskmessages.UploadFile: self.file_upload,
            slskmessages.FileRequest: self.file_request,
            slskmessages.TransferRequest: self.transfer_request,
            slskmessages.TransferResponse: self.transfer_response,
            slskmessages.QueueUpload: self.queue_upload,
            slskmessages.QueueFailed: self.queue_failed,
            slskmessages.UploadFailed: self.upload_failed,
            slskmessages.PlaceInQueue: self.place_in_queue,
            slskmessages.FileError: self.file_error,
            slskmessages.FolderContentsResponse: self.folder_contents_response,
            slskmessages.FolderContentsRequest: self.folder_contents_request,
            slskmessages.RoomList: self.room_list,
            slskmessages.LeaveRoom: self.leave_room,
            slskmessages.GlobalUserList: self.global_user_list,
            slskmessages.AddUser: self.add_user,
            slskmessages.PrivilegedUsers: self.privileged_users,
            slskmessages.AddToPrivileged: self.add_to_privileged,
            slskmessages.CheckPrivileges: self.check_privileges,
            slskmessages.ServerPing: self.dummy_message,
            slskmessages.ParentMinSpeed: self.dummy_message,
            slskmessages.ParentSpeedRatio: self.dummy_message,
            slskmessages.ParentInactivityTimeout: self.dummy_message,
            slskmessages.SearchInactivityTimeout: self.dummy_message,
            slskmessages.MinParentsInCache: self.dummy_message,
            slskmessages.WishlistInterval: self.wishlist_interval,
            slskmessages.DistribAliveInterval: self.dummy_message,
            slskmessages.ChildDepth: self.child_depth,
            slskmessages.BranchLevel: self.branch_level,
            slskmessages.BranchRoot: self.branch_root,
            slskmessages.DistribChildDepth: self.distrib_child_depth,
            slskmessages.DistribBranchLevel: self.distrib_branch_level,
            slskmessages.DistribBranchRoot: self.distrib_branch_root,
            slskmessages.AdminMessage: self.admin_message,
            slskmessages.TunneledMessage: self.tunneled_message,
            slskmessages.IncConn: self.inc_conn,
            slskmessages.PlaceholdUpload: self.dummy_message,
            slskmessages.PlaceInQueueRequest: self.place_in_queue_request,
            slskmessages.UploadQueueNotification: self.upload_queue_notification,
            slskmessages.SearchRequest: self.search_request,
            slskmessages.FileSearch: self.search_request,
            slskmessages.RoomSearch: self.room_search_request,
            slskmessages.UserSearch: self.search_request,
            slskmessages.PossibleParents: self.possible_parents,
            slskmessages.DistribAlive: self.dummy_message,
            slskmessages.DistribSearch: self.distrib_search,
            slskmessages.DistribServerSearch: self.distrib_search,
            ConnectToPeerTimeout: self.connect_to_peer_timeout,
            transfers.TransferTimeout: self.transfer_timeout,
            str: self.notify,
            slskmessages.PopupMessage: self.popup_message,
            slskmessages.SetCurrentConnectionCount: self.set_current_connection_count,
            slskmessages.GlobalRecommendations: self.global_recommendations,
            slskmessages.Recommendations: self.recommendations,
            slskmessages.ItemRecommendations: self.item_recommendations,
            slskmessages.SimilarUsers: self.similar_users,
            slskmessages.ItemSimilarUsers: self.similar_users,
            slskmessages.UserInterests: self.user_interests,
            slskmessages.RoomTickerState: self.room_ticker_state,
            slskmessages.RoomTickerAdd: self.room_ticker_add,
            slskmessages.RoomTickerRemove: self.room_ticker_remove,
            slskmessages.UserPrivileged: self.user_privileged,
            slskmessages.AckNotifyPrivileges: self.ack_notify_privileges,
            slskmessages.NotifyPrivileges: self.notify_privileges,
            slskmessages.PrivateRoomUsers: self.private_room_users,
            slskmessages.PrivateRoomOwned: self.private_room_owned,
            slskmessages.PrivateRoomAddUser: self.private_room_add_user,
            slskmessages.PrivateRoomRemoveUser: self.private_room_remove_user,
            slskmessages.PrivateRoomAdded: self.private_room_added,
            slskmessages.PrivateRoomRemoved: self.private_room_removed,
            slskmessages.PrivateRoomDisown: self.private_room_disown,
            slskmessages.PrivateRoomToggle: self.private_room_toggle,
            slskmessages.PrivateRoomSomething: self.dummy_message,
            slskmessages.PrivateRoomOperatorAdded: self.private_room_operator_added,
            slskmessages.PrivateRoomOperatorRemoved: self.private_room_operator_removed,
            slskmessages.PrivateRoomAddOperator: self.private_room_add_operator,
            slskmessages.PrivateRoomRemoveOperator: self.private_room_remove_operator,
            slskmessages.PublicRoomMessage: self.public_room_message,
            slskmessages.UnknownPeerMessage: self.dummy_message,
        }

    def process_request_to_peer(self, user, message, window=None, address=None):
        """
        Sends message to a peer and possibly sets up a window to display
        the result.
        """

        conn = None

        if message.__class__ is not slskmessages.FileRequest:
            for i in self.peerconns:
                if i.username == user and i.type == 'P':
                    conn = i
                    break

        if conn is not None and conn.conn is not None:

            message.conn = conn.conn

            self.queue.put(message)

            if window is not None:
                window.init_window(conn.username, conn.conn)

            if message.__class__ is slskmessages.TransferRequest and self.transfers is not None:
                self.transfers.got_connect(message.req, conn.conn, message.direction)

            return

        else:

            if message.__class__ is slskmessages.FileRequest:
                message_type = 'F'
            elif message.__class__ is slskmessages.DistribConn:
                message_type = 'D'
            else:
                message_type = 'P'

            init = slskmessages.PeerInit(None, self.config.sections["server"]["login"], message_type, 0)
            firewalled = self.config.sections["server"]["firewalled"]
            addr = None
            behindfw = None
            token = None

            if user in self.users:
                addr = self.users[user].addr
                behindfw = self.users[user].behindfw
            elif address is not None:
                self.users[user] = UserAddr(status=-1, addr=address)
                addr = address

            if firewalled:
                if addr is None:
                    if user not in self.user_addr_requested:
                        self.queue.put(slskmessages.GetPeerAddress(user))
                        self.user_addr_requested.add(user)
                elif behindfw is None:
                    self.queue.put(slskmessages.OutConn(None, addr))
                else:
                    firewalled = 0

            if not firewalled:
                token = new_id()
                self.queue.put(slskmessages.ConnectToPeer(token, user, message_type))

            conn = PeerConnection(addr=addr, username=user, msgs=[message], token=token, init=init)
            self.peerconns.append(conn)

            if token is not None:
                timeout = 120.0
                conntimeout = ConnectToPeerTimeout(self.peerconns[-1], self.network_callback)
                timer = threading.Timer(timeout, conntimeout.timeout)
                timer.setDaemon(True)
                self.peerconns[-1].conntimer = timer
                timer.start()

        if message.__class__ is slskmessages.TransferRequest and self.transfers is not None:

            if conn.addr is None:
                self.transfers.getting_address(message.req, message.direction)
            elif conn.token is None:
                self.transfers.got_address(message.req, message.direction)
            else:
                self.transfers.got_connect_error(message.req, message.direction)

    def set_server_timer(self):

        if self.server_timeout_value == -1:
            self.server_timeout_value = 15
        elif 0 < self.server_timeout_value < 600:
            self.server_timeout_value = self.server_timeout_value * 2

        self.servertimer = threading.Timer(self.server_timeout_value, self.server_timeout)
        self.servertimer.setDaemon(True)
        self.servertimer.start()

        self.set_status(_("The server seems to be down or not responding, retrying in %i seconds"), (self.server_timeout_value))

    def server_timeout(self):
        if self.config.need_config() <= 1:
            self.network_callback([slskmessages.ConnectToServer()])

    def stop_timers(self):

        for i in self.peerconns:
            if i.conntimer is not None:
                i.conntimer.cancel()

        if self.servertimer is not None:
            self.servertimer.cancel()

        if self.transfers is not None:
            self.transfers.abort_transfers()

    def connect_to_server(self, msg):
        self.ui_callback.on_connect(None)

    # notify user of error when recieving or sending a message
    # @param self NetworkEventProcessor (Class)
    # @param string a string containing an error message
    def notify(self, string):
        log.add_msg_contents("%s", string)

    def contents(self, obj):
        """ Returns variables for object, for debug output """
        try:
            return {s: getattr(obj, s) for s in obj.__slots__ if hasattr(obj, s)}
        except AttributeError:
            return vars(obj)

    def popup_message(self, msg):
        self.set_status(_(msg.title))
        self.ui_callback.popup_message(msg)

    def dummy_message(self, msg):
        log.add_msg_contents("%s %s", (msg.__class__, self.contents(msg)))

    def set_current_connection_count(self, msg):
        self.ui_callback.set_socket_status(msg.msg)

    def connect_error(self, msg):

        if msg.connobj.__class__ is slskmessages.ServerConn:

            self.set_status(
                _("Can't connect to server %(host)s:%(port)s: %(error)s"), {
                    'host': msg.connobj.addr[0],
                    'port': msg.connobj.addr[1],
                    'error': msg.err
                }
            )

            self.set_server_timer()

            if self.active_server_conn is not None:
                self.active_server_conn = None

            self.ui_callback.connect_error(msg)

        elif msg.connobj.__class__ is slskmessages.OutConn:

            addr = msg.connobj.addr

            for i in self.peerconns:

                if i.addr == addr and i.conn is None:

                    if i.token is None:

                        i.token = new_id()
                        self.queue.put(slskmessages.ConnectToPeer(i.token, i.username, i.type))

                        if i.username in self.users:
                            self.users[i.username].behindfw = "yes"

                        for j in i.msgs:
                            if j.__class__ is slskmessages.TransferRequest and self.transfers is not None:
                                self.transfers.got_connect_error(j.req, j.direction)

                        conntimeout = ConnectToPeerTimeout(i, self.network_callback)
                        timer = threading.Timer(120.0, conntimeout.timeout)
                        timer.setDaemon(True)
                        timer.start()

                        if i.conntimer is not None:
                            i.conntimer.cancel()

                        i.conntimer = timer

                    else:
                        for j in i.msgs:
                            if j.__class__ in [slskmessages.TransferRequest, slskmessages.FileRequest] and self.transfers is not None:
                                self.transfers.got_cant_connect(j.req)

                        log.add_conn(_("Can't connect to %s, sending notification via the server"), i.username)
                        self.queue.put(slskmessages.CantConnectToPeer(i.token, i.username))

                        if i.conntimer is not None:
                            i.conntimer.cancel()

                        self.peerconns.remove(i)

                    break
            else:
                log.add_msg_contents("%s %s %s", (msg.err, msg.__class__, self.contents(msg)))

        else:
            log.add_msg_contents("%s %s %s", (msg.err, msg.__class__, self.contents(msg)))

            self.closed_connection(msg.connobj.conn, msg.connobj.addr, msg.err)

    def inc_port(self, msg):
        self.waitport = msg.port
        self.set_status(_("Listening on port %i"), msg.port)

    def server_conn(self, msg):

        self.set_status(
            _("Connected to server %(host)s:%(port)s, logging in..."), {
                'host': msg.addr[0],
                'port': msg.addr[1]
            }
        )

        self.active_server_conn = msg.conn
        self.server_timeout_value = -1
        self.users = {}
        self.queue.put(
            slskmessages.Login(
                self.config.sections["server"]["login"],
                self.config.sections["server"]["passw"],

                # Afaik, the client version was set to 157 ns at some point in the past
                # to support distributed searches properly. Probably no reason to mess
                # with this (yet)

                # Soulseek client version; 155, 156, 157, 180, 181, 183
                157,

                # Soulseek client minor version
                # 17 stands for 157 ns 13c, 19 for 157 ns 13e
                # For client versions newer than 157, the minor version is probably 1
                19,
            )
        )
        if self.waitport is not None:
            self.queue.put(slskmessages.SetWaitPort(self.waitport))

    def peer_init(self, msg):
        self.peerconns.append(
            PeerConnection(
                addr=msg.conn.addr,
                username=msg.user,
                conn=msg.conn.conn,
                init=msg,
                msgs=[]
            )
        )

    def conn_close(self, msg):
        self.closed_connection(msg.conn, msg.addr)

    def closed_connection(self, conn, addr, error=None):

        if conn == self.active_server_conn:

            self.set_status(
                _("Disconnected from server %(host)s:%(port)s"), {
                    'host': addr[0],
                    'port': addr[1]
                }
            )
            userchoice = self.manualdisconnect

            if not self.manualdisconnect:
                self.set_server_timer()
            else:
                self.manualdisconnect = False

            self.active_server_conn = None
            self.watchedusers = []

            if self.transfers is not None:
                self.transfers.abort_transfers()
                self.transfers.save_downloads()

            self.privatechat = self.chatrooms = self.userinfo = self.userbrowse = self.search = self.transfers = self.userlist = None
            self.ui_callback.conn_close(conn, addr)
            self.pluginhandler.server_disconnect_notification(userchoice)

        else:
            for i in self.peerconns:
                if i.conn == conn:
                    log.add_conn(self.conn_close_template, self.contents(i))

                    if i.conntimer is not None:
                        i.conntimer.cancel()

                    if self.transfers is not None:
                        self.transfers.conn_close(conn, addr, i.username, error)

                    if i == self.get_parent_conn():
                        self.parent_conn_closed()

                    self.peerconns.remove(i)
                    break
            else:
                log.add_conn(
                    self.conn_remove_template, {
                        'conn_obj': conn,
                        'address': addr
                    }
                )

    def login(self, msg):

        if msg.success:

            self.transfers = transfers.Transfers(self.peerconns, self.queue, self, self.users,
                                                 self.network_callback, self.ui_callback.notifications, self.pluginhandler)

            if msg.ip is not None:
                self.ipaddress = msg.ip

            self.privatechat, self.chatrooms, self.userinfo, self.userbrowse, self.search, downloads, uploads, self.userlist, self.interests = self.ui_callback.init_interface(msg)

            self.transfers.set_transfer_views(downloads, uploads)
            self.shares.send_num_shared_folders_files()
            self.queue.put(slskmessages.SetStatus((not self.ui_callback.away) + 1))

            for thing in self.config.sections["interests"]["likes"]:
                self.queue.put(slskmessages.AddThingILike(thing))
            for thing in self.config.sections["interests"]["dislikes"]:
                self.queue.put(slskmessages.AddThingIHate(thing))

            self.queue.put(slskmessages.HaveNoParent(1))

            """ TODO: Nicotine+ can currently receive search requests from a parent connection, but
            redirecting results to children is not implemented yet. Tell the server we don't accept
            children for now. """
            self.queue.put(slskmessages.AcceptChildren(0))

            self.queue.put(slskmessages.NotifyPrivileges(1, self.config.sections["server"]["login"]))
            self.privatechat.login()
            self.queue.put(slskmessages.CheckPrivileges())
            self.queue.put(slskmessages.PrivateRoomToggle(self.config.sections["server"]["private_chatrooms"]))
        else:
            self.manualdisconnect = True
            self.set_status(_("Can not log in, reason: %s"), (msg.reason))

    def change_password(self, msg):
        password = msg.password
        self.config.sections["server"]["passw"] = password
        self.config.write_configuration()
        self.network_callback([slskmessages.PopupMessage(_("Your password has been changed"), "Password is %s" % password)])

    def notify_privileges(self, msg):

        if msg.token is not None:
            pass

        log.add_msg_contents("%s %s", (msg.__class__, self.contents(msg)))

    def user_privileged(self, msg):
        if self.transfers is not None:
            if msg.privileged is True:
                self.transfers.add_to_privileged(msg.user)

    def ack_notify_privileges(self, msg):

        if msg.token is not None:
            # Until I know the syntax, sending this message is probably a bad idea
            self.queue.put(slskmessages.AckNotifyPrivileges(msg.token))

        log.add_msg_contents("%s %s", (msg.__class__, self.contents(msg)))

    def p_message_user(self, msg):

        conn = msg.conn.conn
        user = None

        # Get peer's username
        for i in self.peerconns:
            if i.conn is conn:
                user = i.username
                break

        if user is None:
            # No peer connection
            return

        if user != msg.user:
            text = _("(Warning: %(realuser)s is attempting to spoof %(fakeuser)s) ") % {"realuser": user, "fakeuser": msg.user} + msg.msg
            msg.user = user
        else:
            text = msg.msg

        if self.privatechat is not None:
            self.privatechat.show_message(msg, text, status=0)

        log.add_msg_contents("%s %s", (msg.__class__, self.contents(msg)))

    def message_user(self, msg):

        if self.privatechat is not None:

            event = self.pluginhandler.incoming_private_chat_event(msg.user, msg.msg)

            if event is not None:
                (u, msg.msg) = event
                self.privatechat.show_message(msg, msg.msg, msg.newmessage)

                self.pluginhandler.incoming_private_chat_notification(msg.user, msg.msg)

            self.queue.put(slskmessages.MessageAcked(msg.msgid))

        log.add_msg_contents("%s %s", (msg.__class__, self.contents(msg)))

    def user_joined_room(self, msg):

        if self.chatrooms is not None:
            self.chatrooms.roomsctrl.user_joined_room(msg)

        log.add_msg_contents("%s %s", (msg.__class__, self.contents(msg)))

    def public_room_message(self, msg):

        if self.chatrooms is not None:
            self.chatrooms.roomsctrl.public_room_message(msg, msg.msg)
            self.pluginhandler.public_room_message_notification(msg.room, msg.user, msg.msg)
        else:
            log.add_msg_contents("%s %s", (msg.__class__, self.contents(msg)))

    def join_room(self, msg):

        if self.chatrooms is not None:

            self.chatrooms.roomsctrl.join_room(msg)

        log.add_msg_contents("%s %s", (msg.__class__, self.contents(msg)))

    def private_room_users(self, msg):
        if self.chatrooms is not None:
            self.chatrooms.roomsctrl.private_room_users(msg)
        log.add_msg_contents("%s %s", (msg.__class__, self.contents(msg)))

    def private_room_owned(self, msg):
        if self.chatrooms is not None:
            self.chatrooms.roomsctrl.private_room_owned(msg)
        log.add_msg_contents("%s %s", (msg.__class__, self.contents(msg)))

    def private_room_add_user(self, msg):
        if self.chatrooms is not None:
            self.chatrooms.roomsctrl.private_room_add_user(msg)
        log.add_msg_contents("%s %s", (msg.__class__, self.contents(msg)))

    def private_room_remove_user(self, msg):
        if self.chatrooms is not None:
            self.chatrooms.roomsctrl.private_room_remove_user(msg)
        log.add_msg_contents("%s %s", (msg.__class__, self.contents(msg)))

    def private_room_operator_added(self, msg):
        if self.chatrooms is not None:
            self.chatrooms.roomsctrl.private_room_operator_added(msg)
        log.add_msg_contents("%s %s", (msg.__class__, self.contents(msg)))

    def private_room_operator_removed(self, msg):
        if self.chatrooms is not None:
            self.chatrooms.roomsctrl.private_room_operator_removed(msg)
        log.add_msg_contents("%s %s", (msg.__class__, self.contents(msg)))

    def private_room_add_operator(self, msg):
        if self.chatrooms is not None:
            self.chatrooms.roomsctrl.private_room_add_operator(msg)
        log.add_msg_contents("%s %s", (msg.__class__, self.contents(msg)))

    def private_room_remove_operator(self, msg):
        if self.chatrooms is not None:
            self.chatrooms.roomsctrl.private_room_remove_operator(msg)
        log.add_msg_contents("%s %s", (msg.__class__, self.contents(msg)))

    def private_room_added(self, msg):
        if self.chatrooms is not None:
            self.chatrooms.roomsctrl.private_room_added(msg)
        log.add_msg_contents("%s %s", (msg.__class__, self.contents(msg)))

    def private_room_removed(self, msg):
        if self.chatrooms is not None:
            self.chatrooms.roomsctrl.private_room_removed(msg)
        log.add_msg_contents("%s %s", (msg.__class__, self.contents(msg)))

    def private_room_disown(self, msg):
        if self.chatrooms is not None:
            self.chatrooms.roomsctrl.private_room_disown(msg)
        log.add_msg_contents("%s %s", (msg.__class__, self.contents(msg)))

    def private_room_toggle(self, msg):
        if self.chatrooms is not None:
            self.chatrooms.roomsctrl.toggle_private_rooms(msg.enabled)
        log.add_msg_contents("%s %s", (msg.__class__, self.contents(msg)))

    def leave_room(self, msg):
        if self.chatrooms is not None:
            self.chatrooms.roomsctrl.leave_room(msg)
        else:
            log.add_msg_contents("%s %s", (msg.__class__, self.contents(msg)))

    def private_message_queue_add(self, msg, text):

        user = msg.user

        if user not in self.private_message_queue:
            self.private_message_queue[user] = [[msg, text]]
        else:
            self.private_message_queue[user].append([msg, text])

    def private_message_queue_process(self, user):

        if user in self.private_message_queue:
            for data in self.private_message_queue[user][:]:
                msg, text = data
                self.private_message_queue[user].remove(data)
                self.privatechat.show_message(msg, text)

    def ip_ignored(self, address):

        if address is None:
            return True

        ips = self.config.sections["server"]["ipignorelist"]
        s_address = address.split(".")

        for ip in ips:

            # No Wildcard in IP
            if "*" not in ip:
                if address == ip:
                    return True
                continue

            # Wildcard in IP
            parts = ip.split(".")
            seg = 0

            for part in parts:
                # Stop if there's no wildcard or matching string number
                if part not in (s_address[seg], "*"):
                    break

                seg += 1

                # Last time around
                if seg == 4:
                    # Wildcard blocked
                    return True

        # Not blocked
        return False

    def say_chat_room(self, msg):

        if self.chatrooms is not None:
            event = self.pluginhandler.incoming_public_chat_event(msg.room, msg.user, msg.msg)
            if event is not None:
                (r, n, msg.msg) = event
                self.chatrooms.roomsctrl.say_chat_room(msg, msg.msg)
                self.pluginhandler.incoming_public_chat_notification(msg.room, msg.user, msg.msg)
        else:
            log.add_msg_contents("%s %s", (msg.__class__, self.contents(msg)))

    def add_user(self, msg):

        if msg.user not in self.watchedusers:
            self.watchedusers.append(msg.user)

        if not msg.userexists:
            if msg.user not in self.users:
                self.users[msg.user] = UserAddr(status=-1)

        if msg.status is not None:
            self.get_user_status(msg)
        elif msg.userexists and msg.status is None:
            self.queue.put(slskmessages.GetUserStatus(msg.user))

        if msg.files is not None:
            self.get_user_stats(msg)

        log.add_msg_contents("%s %s", (msg.__class__, self.contents(msg)))

    def privileged_users(self, msg):

        if self.transfers is not None:
            self.transfers.set_privileged_users(msg.users)
            log.add(_("%i privileged users"), (len(msg.users)))
            self.queue.put(slskmessages.HaveNoParent(1))
            self.queue.put(slskmessages.AddUser(self.config.sections["server"]["login"]))
            self.pluginhandler.server_connect_notification()
        else:
            log.add_msg_contents("%s %s", (msg.__class__, self.contents(msg)))

    def add_to_privileged(self, msg):
        if self.transfers is not None:
            self.transfers.add_to_privileged(msg.user)
        else:
            log.add_msg_contents("%s %s", (msg.__class__, self.contents(msg)))

    def check_privileges(self, msg):

        mins = msg.seconds // 60
        hours = mins // 60
        days = hours // 24

        if msg.seconds == 0:
            log.add(
                _("You have no privileges left. They are not necessary, but allow your downloads to be queued ahead of non-privileged users.")
            )
        else:
            log.add(
                _("%(days)i days, %(hours)i hours, %(minutes)i minutes, %(seconds)i seconds of download privileges left."), {
                    'days': days,
                    'hours': hours % 24,
                    'minutes': mins % 60,
                    'seconds': msg.seconds % 60
                }
            )

        self.privileges_left = msg.seconds

    def admin_message(self, msg):
        log.add("%s", (msg.msg))

    def child_depth(self, msg):
        # TODO: Implement me
        log.add_msg_contents("%s %s", (msg.__class__, self.contents(msg)))

    def branch_level(self, msg):
        # TODO: Implement me
        log.add_msg_contents("%s %s", (msg.__class__, self.contents(msg)))

    def branch_root(self, msg):
        # TODO: Implement me
        log.add_msg_contents("%s %s", (msg.__class__, self.contents(msg)))

    def distrib_child_depth(self, msg):
        # TODO: Implement me
        log.add_msg_contents("%s %s", (msg.__class__, self.contents(msg)))

    def distrib_branch_root(self, msg):
        # TODO: Implement me
        log.add_msg_contents("%s %s", (msg.__class__, self.contents(msg)))

    def wishlist_interval(self, msg):
        if self.search is not None:
            self.search.wish_list.set_interval(msg)
        else:
            log.add_msg_contents("%s %s", (msg.__class__, self.contents(msg)))

    def get_user_status(self, msg):

        # Causes recursive requests when privileged?
        # self.queue.put(slskmessages.AddUser(msg.user))
        if msg.user in self.users:
            if msg.status == 0:
                self.users[msg.user] = UserAddr(status=msg.status)
            else:
                self.users[msg.user].status = msg.status
        else:
            self.users[msg.user] = UserAddr(status=msg.status)

        if msg.privileged is not None:
            if msg.privileged == 1:
                if self.transfers is not None:
                    self.transfers.add_to_privileged(msg.user)
                else:
                    log.add_msg_contents("%s %s", (msg.__class__, self.contents(msg)))

        if self.interests is not None:
            self.interests.get_user_status(msg)

        if self.userlist is not None:
            self.userlist.get_user_status(msg)

        if self.transfers is not None:
            self.transfers.get_user_status(msg)

        if self.privatechat is not None:
            self.privatechat.get_user_status(msg)

        if self.userinfo is not None:
            self.userinfo.get_user_status(msg)

        if self.userbrowse is not None:
            self.userbrowse.get_user_status(msg)

        if self.chatrooms is not None:
            self.chatrooms.roomsctrl.get_user_status(msg)
        else:
            log.add_msg_contents("%s %s", (msg.__class__, self.contents(msg)))

    def user_interests(self, msg):

        if self.userinfo is not None:
            self.userinfo.show_interests(msg)

        log.add_msg_contents("%s %s", (msg.__class__, self.contents(msg)))

    def get_user_stats(self, msg):

        if msg.user == self.config.sections["server"]["login"]:
            self.speed = msg.avgspeed

        if self.interests is not None:
            self.interests.get_user_stats(msg)

        if self.chatrooms is not None:
            self.chatrooms.roomsctrl.get_user_stats(msg)

        if self.userinfo is not None:
            self.userinfo.get_user_stats(msg)

        if self.userlist is not None:
            self.userlist.get_user_stats(msg)

        stats = {
            'avgspeed': msg.avgspeed,
            'downloadnum': msg.downloadnum,
            'files': msg.files,
            'dirs': msg.dirs,
        }

        self.pluginhandler.user_stats_notification(msg.user, stats)
        log.add_msg_contents("%s %s", (msg.__class__, self.contents(msg)))

    def user_left_room(self, msg):
        if self.chatrooms is not None:
            self.chatrooms.roomsctrl.user_left_room(msg)
        else:
            log.add_msg_contents("%s %s", (msg.__class__, self.contents(msg)))

    def get_peer_address(self, msg):

        user = msg.user

        for i in self.peerconns:
            if i.username == user and i.addr is None:
                if msg.port != 0 or i.tryaddr == 10:
                    if i.tryaddr == 10:
                        log.add_conn(
                            _("Server reported port 0 for the 10th time for user %(user)s, giving up"), {
                                'user': user
                            }
                        )
                    elif i.tryaddr is not None:
                        log.add_conn(
                            _("Server reported non-zero port for user %(user)s after %(tries)i retries"), {
                                'user': user,
                                'tries': i.tryaddr
                            }
                        )

                    if user in self.user_addr_requested:
                        self.user_addr_requested.remove(user)

                    i.addr = (msg.ip, msg.port)
                    i.tryaddr = None

                    self.queue.put(slskmessages.OutConn(None, i.addr))

                    for j in i.msgs:
                        if j.__class__ is slskmessages.TransferRequest and self.transfers is not None:
                            self.transfers.got_address(j.req, j.direction)
                else:
                    if i.tryaddr is None:
                        i.tryaddr = 1
                        log.add_conn(
                            _("Server reported port 0 for user %(user)s, retrying"), {
                                'user': user
                            }
                        )
                    else:
                        i.tryaddr += 1

                    self.queue.put(slskmessages.GetPeerAddress(user))
                    return

        if user in self.users:
            self.users[user].addr = (msg.ip, msg.port)
        else:
            self.users[user] = UserAddr(addr=(msg.ip, msg.port))

        if user in self.ipblock_requested:

            if self.ipblock_requested[user]:
                self.ui_callback.on_un_block_user(user)
            else:
                self.ui_callback.on_block_user(user)

            del self.ipblock_requested[user]
            return

        if user in self.ipignore_requested:

            if self.ipignore_requested[user]:
                self.ui_callback.on_un_ignore_user(user)
            else:
                self.ui_callback.on_ignore_user(user)

            del self.ipignore_requested[user]
            return

        ip_record = self.geoip.get_all(msg.ip)
        cc = ip_record.country_short

        if cc == "-":
            cc = ""

        self.ui_callback.has_user_flag(user, "flag_" + cc)

        # From this point on all paths should call
        # self.pluginhandler.user_resolve_notification precisely once
        if user in self.private_message_queue:
            self.private_message_queue_process(user)
        if user not in self.ip_requested:
            self.pluginhandler.user_resolve_notification(user, msg.ip, msg.port)
            return

        self.ip_requested.remove(user)
        self.pluginhandler.user_resolve_notification(user, msg.ip, msg.port, cc)

        if cc != "":
            country = " (%(cc)s / %(country)s)" % {'cc': cc, 'country': ip_record.country_long}
        else:
            country = ""

        log.add(_("IP address of %(user)s is %(ip)s, port %(port)i%(country)s"), {
            'user': user,
            'ip': msg.ip,
            'port': msg.port,
            'country': country
        })

    def relogged(self, msg):
        log.add(_("Someone else is logging in with the same nickname, server is going to disconnect us"))
        self.manualdisconnect = True
        self.pluginhandler.server_disconnect_notification(False)

    def out_conn(self, msg):

        addr = msg.addr

        for i in self.peerconns:

            if i.addr == addr and i.conn is None:
                conn = msg.conn

                if i.token is None:
                    i.init.conn = conn
                    self.queue.put(i.init)
                else:
                    self.queue.put(slskmessages.PierceFireWall(conn, i.token))

                i.conn = conn

                for j in i.msgs:

                    if j.__class__ is slskmessages.UserInfoRequest and self.userinfo is not None:
                        self.userinfo.init_window(i.username, conn)

                    if j.__class__ is slskmessages.GetSharedFileList and self.userbrowse is not None:
                        self.userbrowse.init_window(i.username, conn)

                    if j.__class__ is slskmessages.FileRequest and self.transfers is not None:
                        self.transfers.got_file_connect(j.req, conn)

                    if j.__class__ is slskmessages.TransferRequest and self.transfers is not None:
                        self.transfers.got_connect(j.req, conn, j.direction)

                    j.conn = conn
                    self.queue.put(j)

                i.msgs = []
                break

        log.add_conn("%s %s", (msg.__class__, self.contents(msg)))

    def inc_conn(self, msg):
        log.add_conn("%s %s", (msg.__class__, self.contents(msg)))

    def connect_to_peer(self, msg):
        user = msg.user
        ip = msg.ip
        port = msg.port

        init = slskmessages.PeerInit(None, user, msg.type, 0)

        self.queue.put(slskmessages.OutConn(None, (ip, port), init))
        self.peerconns.append(
            PeerConnection(
                addr=(ip, port),
                username=user,
                msgs=[],
                token=msg.token,
                init=init
            )
        )
        log.add_conn("%s %s", (msg.__class__, self.contents(msg)))

    def check_user(self, user, addr):
        """
        Check if this user is banned, geoip-blocked, and which shares
        it is allowed to access based on transfer and shares settings.
        """

        if user in self.config.sections["server"]["banlist"]:
            if self.config.sections["transfers"]["usecustomban"]:
                return 0, "Banned (%s)" % self.config.sections["transfers"]["customban"]
            else:
                return 0, "Banned"

        if user in [i[0] for i in self.config.sections["server"]["userlist"]] and self.config.sections["transfers"]["enablebuddyshares"]:
            # For sending buddy-only shares
            return 2, ""

        if user in [i[0] for i in self.config.sections["server"]["userlist"]]:
            return 1, ""

        if self.config.sections["transfers"]["friendsonly"]:
            return 0, "Sorry, friends only"

        if not self.config.sections["transfers"]["geoblock"]:
            return 1, ""

        cc = "-"
        if addr is not None:
            cc = self.geoip.get_all(addr).country_short

        if cc == "-":
            if self.config.sections["transfers"]["geopanic"]:
                return 0, "Sorry, geographical paranoia"
            else:
                return 1, ""

        if self.config.sections["transfers"]["geoblockcc"][0].find(cc) >= 0:
            return 0, "Sorry, your country is blocked"

        return 1, ""

    def check_spoof(self, user, ip, port):

        if user not in self.users:
            return 0

        if self.users[user].addr is not None:

            if len(self.users[user].addr) == 2:
                if self.users[user].addr is not None:
                    u_ip, u_port = self.users[user].addr
                    if u_ip != ip:
                        log.add_warning(_("IP %(ip)s:%(port)s is spoofing user %(user)s with a peer request, blocking because it does not match IP: %(real_ip)s"), {
                            'ip': ip,
                            'port': port,
                            'user': user,
                            'real_ip': u_ip
                        })
                        return 1
        return 0

    def close_peer_connection(self, peerconn):
        try:
            conn = peerconn.conn
        except AttributeError:
            conn = peerconn

        if conn is None:
            return

        if not self.protothread.socket_still_active(conn):
            self.queue.put(slskmessages.ConnClose(conn))

            if isinstance(peerconn, socket):

                for i in self.peerconns:
                    if i.conn == peerconn:
                        self.peerconns.remove(i)
                        break
            else:
                try:
                    self.peerconns.remove(peerconn)
                except ValueError:
                    pass

    def user_info_reply(self, msg):
        conn = msg.conn.conn

        for i in self.peerconns:
            if i.conn is conn and self.userinfo is not None:
                # probably impossible to do this
                if i.username != self.config.sections["server"]["login"]:
                    self.userinfo.show_info(i.username, msg)
                    break

    def user_info_request(self, msg):

        user = ip = port = None
        conn = msg.conn.conn

        # Get peer's username, ip and port
        for i in self.peerconns:
            if i.conn is conn:
                user = i.username
                if i.addr is not None:
                    ip, port = i.addr
                break

        if user is None:
            # No peer connection
            return

        request_time = time.time()

        if user in self.requested_info:
            if not request_time > 10 + self.requested_info[user]:
                # Ignoring request, because it's 10 or less seconds since the
                # last one by this user
                return

        self.requested_info[user] = request_time

        # Check address is spoofed, if possible
        if user == self.config.sections["server"]["login"]:

            if ip is not None and port is not None:
                log.add(
                    _("Blocking %(user)s from making a UserInfo request, possible spoofing attempt from IP %(ip)s port %(port)s"), {
                        'user': user,
                        'ip': ip,
                        'port': port
                    }
                )
            else:
                log.add(_("Blocking %s from making a UserInfo request, possible spoofing attempt from an unknown IP & port"), user)

            if conn is not None:
                self.queue.put(slskmessages.ConnClose(conn))

            return

        if user in self.config.sections["server"]["banlist"]:

            log.add_warning(
                _("%(user)s is banned, but is making a UserInfo request"), {
                    'user': user
                }
            )

            log.add_warning("%s %s", (msg.__class__, self.contents(msg)))

            return

        try:
            userpic = self.config.sections["userinfo"]["pic"]

            with open(userpic, 'rb') as f:
                pic = f.read()

        except Exception:
            pic = None

        descr = unescape(self.config.sections["userinfo"]["descr"])

        if self.transfers is not None:
            totalupl = self.transfers.get_total_uploads_allowed()
            queuesize = self.transfers.get_upload_queue_sizes()[0]
            slotsavail = self.transfers.allow_new_uploads()

            if self.config.sections["transfers"]["remotedownloads"]:
                uploadallowed = self.config.sections["transfers"]["uploadallowed"]
            else:
                uploadallowed = 0

            self.queue.put(slskmessages.UserInfoReply(conn, descr, pic, totalupl, queuesize, slotsavail, uploadallowed))

        log.add(
            _("%(user)s is making a UserInfo request"), {
                'user': user
            }
        )

    def shared_file_list(self, msg):
        conn = msg.conn.conn

        for i in self.peerconns:
            if i.conn is conn and self.userbrowse is not None:
                if i.username != self.config.sections["server"]["login"]:
                    self.userbrowse.show_info(i.username, msg)
                    break

    def file_search_result(self, msg):
        conn = msg.conn
        addr = conn.addr

        if self.search is not None:
            if addr:
                country = self.geoip.get_all(addr[0]).country_short
            else:
                country = ""

            if country == "-":
                country = ""

            self.search.show_result(msg, msg.user, country)
            self.close_peer_connection(conn)

        log.add_msg_contents("%s %s", (msg.__class__, self.contents(msg)))

    def pierce_fire_wall(self, msg):
        token = msg.token

        for i in self.peerconns:

            if i.token == token and i.conn is None:
                conn = msg.conn.conn

                if i.conntimer is not None:
                    i.conntimer.cancel()

                i.init.conn = conn
                self.queue.put(i.init)
                i.conn = conn

                for j in i.msgs:

                    if j.__class__ is slskmessages.UserInfoRequest and self.userinfo is not None:
                        self.userinfo.init_window(i.username, conn)

                    if j.__class__ is slskmessages.GetSharedFileList and self.userbrowse is not None:
                        self.userbrowse.init_window(i.username, conn)

                    if j.__class__ is slskmessages.FileRequest and self.transfers is not None:
                        self.transfers.got_file_connect(j.req, conn)

                    if j.__class__ is slskmessages.TransferRequest and self.transfers is not None:
                        self.transfers.got_connect(j.req, conn, j.direction)

                    j.conn = conn
                    self.queue.put(j)

                i.msgs = []
                break

        log.add_conn("%s %s", (msg.__class__, self.contents(msg)))

    def cant_connect_to_peer(self, msg):
        token = msg.token

        for i in self.peerconns:

            if i.token == token:

                if i.conntimer is not None:
                    i.conntimer.cancel()

                if i == self.get_parent_conn():
                    self.parent_conn_closed()

                self.peerconns.remove(i)

                log.add_conn(_("Can't connect to %s (either way), giving up"), i.username)

                for j in i.msgs:
                    if j.__class__ in [slskmessages.TransferRequest, slskmessages.FileRequest] and self.transfers is not None:
                        self.transfers.got_cant_connect(j.req)
                break

    def connect_to_peer_timeout(self, msg):
        conn = msg.conn

        if conn == self.get_parent_conn():
            self.parent_conn_closed()

        try:
            self.peerconns.remove(conn)
        except ValueError:
            pass

        log.add_conn(_("User %s does not respond to connect request, giving up"), conn.username)

        for i in conn.msgs:
            if i.__class__ in [slskmessages.TransferRequest, slskmessages.FileRequest] and self.transfers is not None:
                self.transfers.got_cant_connect(i.req)

    def transfer_timeout(self, msg):
        if self.transfers is not None:
            self.transfers.transfer_timeout(msg)
        else:
            log.add_msg_contents("%s %s", (msg.__class__, self.contents(msg)))

    def file_download(self, msg):
        if self.transfers is not None:
            self.transfers.file_download(msg)
        else:
            log.add_msg_contents("%s %s", (msg.__class__, self.contents(msg)))

    def file_upload(self, msg):
        if self.transfers is not None:
            self.transfers.file_upload(msg)
        else:
            log.add_msg_contents("%s %s", (msg.__class__, self.contents(msg)))

    def file_request(self, msg):
        if self.transfers is not None:
            self.transfers.file_request(msg)
        else:
            log.add_msg_contents("%s %s", (msg.__class__, self.contents(msg)))

    def file_error(self, msg):
        if self.transfers is not None:
            self.transfers.file_error(msg)
        else:
            log.add_msg_contents("%s %s", (msg.__class__, self.contents(msg)))

    def transfer_request(self, msg):
        """ Peer code: 40 """

        if self.transfers is not None:
            self.transfers.transfer_request(msg)
        else:
            log.add_msg_contents("%s %s", (msg.__class__, self.contents(msg)))

    def transfer_response(self, msg):
        """ Peer code: 41 """

        if self.transfers is not None:
            self.transfers.transfer_response(msg)
        else:
            log.add_msg_contents("%s %s", (msg.__class__, self.contents(msg)))

    def queue_upload(self, msg):
        """ Peer code: 43 """

        if self.transfers is not None:
            self.transfers.queue_upload(msg)
        else:
            log.add_msg_contents("%s %s", (msg.__class__, self.contents(msg)))

    def queue_failed(self, msg):
        """ Peer code: 50 """

        if self.transfers is not None:
            self.transfers.queue_failed(msg)
        else:
            log.add_msg_contents("%s %s", (msg.__class__, self.contents(msg)))

    def place_in_queue_request(self, msg):
        """ Peer code: 51 """

        if self.transfers is not None:
            self.transfers.place_in_queue_request(msg)
        else:
            log.add_msg_contents("%s %s", (msg.__class__, self.contents(msg)))

    def upload_queue_notification(self, msg):
        """ Peer code: 52 """

        log.add_msg_contents("%s %s", (msg.__class__, self.contents(msg)))
        self.transfers.upload_queue_notification(msg)

    def upload_failed(self, msg):
        """ Peer code: 46 """

        if self.transfers is not None:
            self.transfers.upload_failed(msg)
        else:
            log.add_msg_contents("%s %s", (msg.__class__, self.contents(msg)))

    def place_in_queue(self, msg):
        """ Peer code: 44 """

        if self.transfers is not None:
            self.transfers.place_in_queue(msg)
        else:
            log.add_msg_contents("%s %s", (msg.__class__, self.contents(msg)))

    def get_shared_file_list(self, msg):
        """ Peer code: 4 """

        log.add_msg_contents("%s %s", (msg.__class__, self.contents(msg)))
        user = ip = port = None
        conn = msg.conn.conn

        # Get peer's username, ip and port
        for i in self.peerconns:
            if i.conn is conn:
                user = i.username
                if i.addr is not None:
                    if len(i.addr) != 2:
                        break
                    ip, port = i.addr
                break

        if user is None:
            # No peer connection
            return

        # Check address is spoofed, if possible
        # if self.check_spoof(user, ip, port):
        #     # Message IS spoofed
        #     return
        if user == self.config.sections["server"]["login"]:
            if ip is not None and port is not None:
                log.add(
                    _("%(user)s is making a BrowseShares request, blocking possible spoofing attempt from IP %(ip)s port %(port)s"), {
                        'user': user,
                        'ip': ip,
                        'port': port
                    })
            else:
                log.add(
                    _("%(user)s is making a BrowseShares request, blocking possible spoofing attempt from an unknown IP & port"), {
                        'user': user
                    })

            if conn is not None:
                self.queue.put(slskmessages.ConnClose(conn))
            return

        log.add(_("%(user)s is making a BrowseShares request"), {
            'user': user
        })

        addr = msg.conn.addr[0]
        checkuser, reason = self.check_user(user, addr)

        if checkuser == 1:
            # Send Normal Shares
            if self.shares.newnormalshares:
                self.shares.compress_shares("normal")
                self.shares.newnormalshares = False
            m = self.shares.compressed_shares_normal

        elif checkuser == 2:
            # Send Buddy Shares
            if self.shares.newbuddyshares:
                self.shares.compress_shares("buddy")
                self.shares.newbuddyshares = False
            m = self.shares.compressed_shares_buddy

        else:
            # Nyah, Nyah
            m = slskmessages.SharedFileList(conn, {})
            m.make_network_message(nozlib=0)

        m.conn = conn
        self.queue.put(m)

    def folder_contents_request(self, msg):
        """ Peer code: 36 """

        conn = msg.conn.conn
        username = None
        checkuser = None
        reason = ""

        for i in self.peerconns:
            if i.conn is conn:
                username = i.username
                checkuser, reason = self.check_user(username, None)
                break

        if not username:
            return
        if not checkuser:
            self.queue.put(slskmessages.MessageUser(username, "[Automatic Message] " + reason))
            return

        if checkuser == 1:
            shares = self.config.sections["transfers"]["sharedfilesstreams"]
        elif checkuser == 2:
            shares = self.config.sections["transfers"]["bsharedfilesstreams"]
        else:
            self.queue.put(slskmessages.TransferResponse(conn, 0, reason=reason, req=0))
            shares = {}

        if checkuser:
            if msg.dir in shares:
                self.queue.put(slskmessages.FolderContentsResponse(conn, msg.dir, shares[msg.dir]))
            elif msg.dir.rstrip('\\') in shares:
                self.queue.put(slskmessages.FolderContentsResponse(conn, msg.dir, shares[msg.dir.rstrip('\\')]))
            else:
                if checkuser == 2:
                    shares = self.config.sections["transfers"]["sharedfilesstreams"]
                    if msg.dir in shares:
                        self.queue.put(slskmessages.FolderContentsResponse(conn, msg.dir, shares[msg.dir]))
                    elif msg.dir.rstrip("\\") in shares:
                        self.queue.put(slskmessages.FolderContentsResponse(conn, msg.dir, shares[msg.dir.rstrip("\\")]))

        log.add_msg_contents("%s %s", (msg.__class__, self.contents(msg)))

    def folder_contents_response(self, msg):
        """ Peer code: 37 """

        if self.transfers is not None:
            conn = msg.conn.conn
            file_list = msg.list

            # Check for a large number of files
            many = False
            folder = ""

            for i in file_list:
                for j in file_list[i]:
                    if os.path.commonprefix([i, j]) == j:
                        numfiles = len(file_list[i][j])
                        if numfiles > 100:
                            many = True
                            folder = j

            if many:
                for i in self.peerconns:
                    if i.conn is conn:
                        username = i.username
                        break

                self.transfers.downloadsview.download_large_folder(username, folder, numfiles, conn, file_list)
            else:
                self.transfers.folder_contents_response(conn, file_list)
        else:
            log.add_msg_contents("%s %s", (msg.__class__, self.contents(msg)))

    def room_list(self, msg):
        """ Server code: 64 """

        if self.chatrooms is not None:
            self.chatrooms.roomsctrl.set_room_list(msg)
            self.set_status("")
        else:
            log.add_msg_contents("%s %s", (msg.__class__, self.contents(msg)))

    def global_user_list(self, msg):
        """ Server code: 67 """

        if self.globallist is not None:
            self.globallist.set_global_users_list(msg)
        else:
            log.add_msg_contents("%s %s", (msg.__class__, self.contents(msg)))

    def peer_transfer(self, msg):
        if self.userinfo is not None and msg.msg is slskmessages.UserInfoReply:
            self.userinfo.update_gauge(msg)
        if self.userbrowse is not None and msg.msg is slskmessages.SharedFileList:
            self.userbrowse.update_gauge(msg)

    def tunneled_message(self, msg):
        """ Server code: 68 """
        """ DEPRECATED """

        if msg.code in self.protothread.peerclasses:
            peermsg = self.protothread.peerclasses[msg.code](None)
            peermsg.parse_network_message(msg.msg)
            peermsg.tunneleduser = msg.user
            peermsg.tunneledreq = msg.req
            peermsg.tunneledaddr = msg.addr
            self.network_callback([peermsg])
        else:
            log.add_msg_contents(_("Unknown tunneled message: %s"), (self.contents(msg)))

    def file_search_request(self, msg):
        """ Peer code: 8 """
        conn = msg.conn.conn

        for i in self.peerconns:
            if i.conn == conn:
                user = i.username
                self.shares.process_search_request(msg.searchterm, user, msg.searchid, direct=1)
                break

        log.add_msg_contents("%s %s", (msg.__class__, self.contents(msg)))

    def search_request(self, msg):
        """ Server code: 93 """

        self.shares.process_search_request(msg.searchterm, msg.user, msg.searchid, direct=0)
        self.pluginhandler.search_request_notification(msg.searchterm, msg.user, msg.searchid)
        log.add_msg_contents("%s %s", (msg.__class__, self.contents(msg)))

    def room_search_request(self, msg):
        log.add_msg_contents("%s %s", (msg.__class__, self.contents(msg)))
        self.shares.process_search_request(msg.searchterm, msg.room, msg.searchid, direct=0)

    def distrib_search(self, msg):
        """ Distrib code: 3 """

        self.shares.process_search_request(msg.searchterm, msg.user, msg.searchid, 0)
        self.pluginhandler.distrib_search_notification(msg.searchterm, msg.user, msg.searchid)

    def possible_parents(self, msg):
        """ Server code: 102 """

        """ Server sent a list of 10 potential parents, whose purpose is to forward us search requests.
        We attempt to connect to them all at once, since connection errors are fairly common. """

        potential_parents = msg.list

        if potential_parents:

            for user in potential_parents:
                addr = potential_parents[user]

                self.process_request_to_peer(user, slskmessages.DistribConn(), None, addr)

        log.add_msg_contents("%s %s", (msg.__class__, self.contents(msg)))

    def get_parent_conn(self):
        for i in self.peerconns:
            if i.type == 'D':
                return i

        return None

    def parent_conn_closed(self):
        """ Tell the server it needs to send us a NetInfo message with a new list of
        potential parents. """

        self.has_parent = False
        self.queue.put(slskmessages.HaveNoParent(1))

    def distrib_branch_level(self, msg):
        """ Distrib code: 4 """

        """ This message is received when we have a successful connection with a potential
        parent. Tell the server who our parent is, and stop requesting new potential parents. """

        if not self.has_parent:

            for i in self.peerconns[:]:
                if i.type == 'D':
                    """ We previously attempted to connect to all potential parents. Since we now
                    have a parent, stop connecting to the others. """

                    if i.conn != msg.conn.conn:
                        if i.conn is not None:
                            self.queue.put(slskmessages.ConnClose(i.conn))

                        self.peerconns.remove(i)

            parent = self.get_parent_conn()

            if parent is not None:
                self.queue.put(slskmessages.HaveNoParent(0))
                self.queue.put(slskmessages.SearchParent(msg.conn.addr[0]))
                self.has_parent = True
            else:
                self.parent_conn_closed()

        log.add_msg_contents("%s %s", (msg.__class__, self.contents(msg)))

    def global_recommendations(self, msg):
        """ Server code: 56 """

        if self.interests is not None:
            self.interests.global_recommendations(msg)

    def recommendations(self, msg):
        """ Server code: 54 """

        if self.interests is not None:
            self.interests.recommendations(msg)

    def item_recommendations(self, msg):
        """ Server code: 111 """

        if self.interests is not None:
            self.interests.item_recommendations(msg)

    def similar_users(self, msg):
        """ Server code: 110 """

        if self.interests is not None:
            self.interests.similar_users(msg)

    def room_ticker_state(self, msg):
        """ Server code: 113 """

        if self.chatrooms is not None:
            self.chatrooms.roomsctrl.ticker_set(msg)

        log.add_msg_contents("%s %s", (msg.__class__, self.contents(msg)))

    def room_ticker_add(self, msg):
        """ Server code: 114 """

        if self.chatrooms is not None:
            self.chatrooms.roomsctrl.ticker_add(msg)

        log.add_msg_contents("%s %s", (msg.__class__, self.contents(msg)))

    def room_ticker_remove(self, msg):
        """ Server code: 115 """

        if self.chatrooms is not None:
            self.chatrooms.roomsctrl.ticker_remove(msg)

        log.add_msg_contents("%s %s", (msg.__class__, self.contents(msg)))

    def update_debug_log_options(self):
        """ Gives the logger updated logging settings """

        should_log = self.config.sections["logging"]["debug_file_output"]
        log_folder = self.config.sections["logging"]["debuglogsdir"]
        timestamp_format = self.config.sections["logging"]["log_timestamp"]

        log.set_log_to_file(should_log)
        log.set_folder(log_folder)
        log.set_timestamp_format(timestamp_format)


class UserAddr:

    def __init__(self, addr=None, behindfw=None, status=None):
        self.addr = addr
        self.behindfw = behindfw
        self.status = status
