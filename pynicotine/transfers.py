# COPYRIGHT (C) 2020 Nicotine+ Team
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

""" This module contains classes that deal with file transfers:
the transfer manager.
"""

import hashlib
import os
import os.path
import re
import shutil
import stat
import threading
import time

from gettext import gettext as _
from time import sleep

from pynicotine import slskmessages
from pynicotine.logfacility import log
from pynicotine.slskmessages import new_id
from pynicotine.utils import execute_command
from pynicotine.utils import clean_file
from pynicotine.utils import clean_path
from pynicotine.utils import get_result_bitrate_length
from pynicotine.utils import write_log


class Transfer(object):
    """ This class holds information about a single transfer. """

    __slots__ = "conn", "user", "realfilename", "filename", \
                "path", "req", "size", "file", "starttime", "lasttime", \
                "offset", "currentbytes", "lastbytes", "speed", "timeelapsed", \
                "timeleft", "timequeued", "transfertimer", "requestconn", \
                "modifier", "place", "bitrate", "length", "iter", "_status", \
                "laststatuschange"

    def __init__(
        self, conn=None, user=None, realfilename=None, filename=None,
        path=None, status=None, req=None, size=None, file=None, starttime=None,
        offset=None, currentbytes=None, speed=None, timeelapsed=None,
        timeleft=None, timequeued=None, transfertimer=None, requestconn=None,
        modifier=None, place=0, bitrate=None, length=None, iter=None
    ):
        self.user = user
        self.realfilename = realfilename  # Sent as is to the user announcing what file we're sending
        self.filename = filename
        self.conn = conn
        self.path = path  # Used for ???
        self.modifier = modifier
        self.req = req
        self.size = size
        self.file = file
        self.starttime = starttime
        self.lasttime = starttime
        self.offset = offset
        self.currentbytes = currentbytes
        self.lastbytes = currentbytes
        self.speed = speed
        self.timeelapsed = timeelapsed
        self.timeleft = timeleft
        self.timequeued = timequeued
        self.transfertimer = transfertimer
        self.requestconn = None
        self.place = place  # Queue position
        self.bitrate = bitrate
        self.length = length
        self.iter = iter
        self.setstatus(status)

    def setstatus(self, status):
        self._status = status
        self.laststatuschange = time.time()

    def getstatus(self):
        return self._status
    status = property(getstatus, setstatus)


class TransferTimeout:

    __slots__ = "req", "callback"

    def __init__(self, req, callback):
        self.req = req
        self.callback = callback

    def timeout(self):
        self.callback([self])


class Transfers:
    """ This is the transfers manager"""
    FAILED_TRANSFERS = ["Cannot connect", "Connection closed by peer", "Local file error", "Remote file error"]
    COMPLETED_TRANSFERS = ["Finished", "Filtered", "Aborted", "Cancelled"]
    PRE_TRANSFER = ["Queued"]
    TRANSFER = ["Requesting file", "Initializing transfer", "Transferring"]

    def __init__(self, peerconns, queue, eventprocessor, users, network_callback, notifications=None, pluginhandler=None):

        self.peerconns = peerconns
        self.queue = queue
        self.eventprocessor = eventprocessor
        self.downloads = []
        self.uploads = []
        self.privilegedusers = set()
        self.requested_upload_queue = []
        getstatus = {}

        for i in self.eventprocessor.config.sections["transfers"]["downloads"]:
            size = currentbytes = bitrate = length = None

            if len(i) >= 6:
                try:
                    size = int(i[4])
                except Exception:
                    pass

                try:
                    currentbytes = int(i[5])
                except Exception:
                    pass

            if len(i) >= 8:
                try:
                    bitrate = i[6]
                except Exception:
                    pass

                try:
                    length = i[7]
                except Exception:
                    pass

            if len(i) >= 4 and i[3] in ("Aborted", "Paused"):
                status = "Paused"
            elif len(i) >= 4 and i[3] == "Filtered":
                status = "Filtered"
            else:
                status = "Getting status"

            self.downloads.append(
                Transfer(
                    user=i[0], filename=i[1], path=i[2], status=status,
                    size=size, currentbytes=currentbytes, bitrate=bitrate,
                    length=length
                )
            )
            getstatus[i[0]] = ""

        for i in getstatus:
            if i not in self.eventprocessor.watchedusers:
                self.queue.put(slskmessages.AddUser(i))

        self.users = users
        self.network_callback = network_callback
        self.notifications = notifications
        self.pluginhandler = pluginhandler
        self.downloadsview = None
        self.uploadsview = None

        # queue sizes
        self.privcount = 0
        self.usersqueued = {}
        self.privusersqueued = {}
        self.geoip = self.eventprocessor.geoip

        # Check for failed downloads if option is enabled (1 min delay)
        self.start_check_download_queue_timer()

    def set_transfer_views(self, downloads, uploads):
        self.downloadsview = downloads
        self.uploadsview = uploads

    def set_privileged_users(self, list):
        for i in list:
            self.add_to_privileged(i)

    def add_to_privileged(self, user):

        self.privilegedusers.add(user)

        if user in self.usersqueued:
            self.privusersqueued.setdefault(user, 0)
            self.privusersqueued[user] += self.usersqueued[user]
            self.privcount += self.usersqueued[user]
            del self.usersqueued[user]

    def get_user_status(self, msg):
        """ We get a status of a user and if he's online, we request a file from him """

        for i in self.downloads:
            if msg.user == i.user and i.status in ["Queued", "Getting status", "User logged off", "Connection closed by peer", "Aborted", "Cannot connect", "Paused"]:
                if msg.status != 0:
                    if i.status not in ["Queued", "Aborted", "Cannot connect", "Paused"]:
                        self.get_file(i.user, i.filename, i.path, i)
                else:
                    if i.status not in ["Aborted", "Filtered"]:
                        i.status = "User logged off"
                        self.downloadsview.update(i)

        for i in self.uploads[:]:
            if msg.user == i.user and i.status != "Finished":
                if msg.status != 0:
                    if i.status == "Getting status":
                        self.push_file(i.user, i.filename, i.realfilename, i.path, i)
                else:
                    if i.transfertimer is not None:
                        i.transfertimer.cancel()
                    self.uploads.remove(i)
                    self.uploadsview.remove_specific(i, True)

        if msg.status == 0:
            self.check_upload_queue()

    def get_file(self, user, filename, path="", transfer=None, size=None, bitrate=None, length=None, checkduplicate=False):
        path = clean_path(path, absolute=True)

        if checkduplicate:
            for i in self.downloads:
                if i.user == user and i.filename == filename and i.path == path:
                    # Don't add duplicate downloads
                    return

        self.transfer_file(0, user, filename, path, transfer, size, bitrate, length)

    def push_file(self, user, filename, realfilename, path="", transfer=None, size=None, bitrate=None, length=None):
        if size is None:
            size = self.get_file_size(realfilename)

        self.transfer_file(1, user, filename, path, transfer, size, bitrate, length, realfilename)

    def transfer_file(self, direction, user, filename, path="", transfer=None, size=None, bitrate=None, length=None, realfilename=None):
        """ Get a single file. path is a local path. if transfer object is
        not None, update it, otherwise create a new one."""
        if transfer is None:
            transfer = Transfer(
                user=user, filename=filename, realfilename=realfilename, path=path,
                status="Getting status", size=size, bitrate=bitrate,
                length=length
            )

            if direction == 0:
                self.downloads.append(transfer)
            else:
                self._append_upload(user, filename, transfer)
        else:
            transfer.status = "Getting status"

        try:
            status = self.users[user].status
        except KeyError:
            status = None

        shouldupdate = True

        if not direction and self.eventprocessor.config.sections["transfers"]["enablefilters"]:
            # Only filter downloads, never uploads!
            try:
                downloadregexp = re.compile(self.eventprocessor.config.sections["transfers"]["downloadregexp"], re.I)
                if downloadregexp.search(filename) is not None:
                    log.add_transfer(_("Filtering: %s"), filename)
                    self.abort_transfer(transfer)
                    # The string to be displayed on the GUI
                    transfer.status = "Filtered"

                    shouldupdate = not self.auto_clear_download(transfer)
            except Exception:
                pass

        if status is None:
            if user not in self.eventprocessor.watchedusers:
                self.queue.put(slskmessages.AddUser(user))
            self.queue.put(slskmessages.GetUserStatus(user))

        if transfer.status != "Filtered":
            transfer.req = new_id()
            realpath = self.eventprocessor.shares.virtual2real(filename)
            request = slskmessages.TransferRequest(None, direction, transfer.req, filename, self.get_file_size(realpath), realpath)
            self.eventprocessor.process_request_to_peer(user, request)

        if shouldupdate:
            if direction == 0:
                self.downloadsview.update(transfer)
            else:
                self.uploadsview.update(transfer)

    def upload_failed(self, msg):

        for i in self.peerconns:
            if i.conn is msg.conn.conn:
                user = i.username
                break
        else:
            return

        for i in self.downloads:
            if i.user == user and i.filename == msg.file and (i.conn is not None or i.status in ["Connection closed by peer", "Establishing connection", "Waiting for download"]):
                self.abort_transfer(i)
                self.get_file(i.user, i.filename, i.path, i)
                self.log_transfer(
                    _("Retrying failed download: user %(user)s, file %(file)s") % {
                        'user': i.user,
                        'file': i.filename
                    },
                    show_ui=1
                )
                break

    def getting_address(self, req, direction):

        if direction == 0:
            for i in self.downloads:
                if i.req == req:
                    i.status = "Getting address"
                    self.downloadsview.update(i)
                    break

        elif direction == 1:

            for i in self.uploads:
                if i.req == req:
                    i.status = "Getting address"
                    self.uploadsview.update(i)
                    break

    def got_address(self, req, direction):
        """ A connection is in progress, we got the address for a user we need
        to connect to."""

        if direction == 0:
            for i in self.downloads:
                if i.req == req:
                    i.status = "Connecting"
                    self.downloadsview.update(i)
                    break

        elif direction == 1:

            for i in self.uploads:
                if i.req == req:
                    i.status = "Connecting"
                    self.uploadsview.update(i)
                    break

    def got_connect_error(self, req, direction):
        """ We couldn't connect to the user, now we are waitng for him to
        connect to us. Note that all this logic is handled by the network
        event processor, we just provide a visual feedback to the user."""

        if direction == 0:
            for i in self.downloads:
                if i.req == req:
                    i.status = "Waiting for peer to connect"
                    self.downloadsview.update(i)
                    break

        elif direction == 1:

            for i in self.uploads:
                if i.req == req:
                    i.status = "Waiting for peer to connect"
                    self.uploadsview.update(i)
                    break

    def got_cant_connect(self, req):
        """ We can't connect to the user, either way. """

        for i in self.downloads:
            if i.req == req:
                self._get_cant_connect_download(i)
                break

        for i in self.uploads:
            if i.req == req:
                self._get_cant_connect_upload(i)
                break

    def _get_cant_connect_download(self, i):

        i.status = "Cannot connect"
        i.req = None
        self.downloadsview.update(i)

        if i.user not in self.eventprocessor.watchedusers:
            self.queue.put(slskmessages.AddUser(i.user))

        self.queue.put(slskmessages.GetUserStatus(i.user))

    def _get_cant_connect_upload(self, i):

        i.status = "Cannot connect"
        i.req = None
        curtime = time.time()

        for j in self.uploads:
            if j.user == i.user:
                j.timequeued = curtime

        self.uploadsview.update(i)

        if i.user not in self.eventprocessor.watchedusers:
            self.queue.put(slskmessages.AddUser(i.user))

        self.queue.put(slskmessages.GetUserStatus(i.user))
        self.check_upload_queue()

    def got_file_connect(self, req, conn):
        """ A transfer connection has been established,
        now exchange initialisation messages."""

        for i in self.downloads:
            if i.req == req:
                i.status = "Initializing transfer"
                self.downloadsview.update(i)
                break

        for i in self.uploads:
            if i.req == req:
                i.status = "Initializing transfer"
                self.uploadsview.update(i)
                break

    def got_connect(self, req, conn, direction):
        """ A connection has been established, now exchange initialisation
        messages."""

        if direction == 0:
            for i in self.downloads:
                if i.req == req:
                    i.status = "Requesting file"
                    i.requestconn = conn
                    self.downloadsview.update(i)
                    break

        elif direction == 1:

            for i in self.uploads:
                if i.req == req:
                    i.status = "Requesting file"
                    i.requestconn = conn
                    self.uploadsview.update(i)
                    break

    def transfer_request(self, msg):

        user = response = None

        if msg.conn is not None:
            for i in self.peerconns:
                if i.conn is msg.conn.conn:
                    user = i.username
                    conn = msg.conn.conn
                    addr = msg.conn.addr[0]
        elif msg.tunneleduser is not None:
            user = msg.tunneleduser
            conn = None
            addr = "127.0.0.1"

        if user is None:
            log.add_transfer(_("Got transfer request %s but cannot determine requestor"), vars(msg))
            return

        if msg.direction == 1:
            response = self.transfer_request_downloads(msg, user, conn, addr)
        else:
            response = self.transfer_request_uploads(msg, user, conn, addr)

        if msg.conn is not None:
            self.queue.put(response)
        else:
            self.eventprocessor.process_request_to_peer(user, response)

    def transfer_request_downloads(self, msg, user, conn, addr):

        for i in self.downloads:
            if i.filename == msg.file and user == i.user and i.status not in ["Aborted", "Paused"]:
                # Remote peer is signalling a tranfer is ready, attempting to download it

                """ If the file is larger than 2GB, the SoulseekQt client seems to
                send a malformed file size (0 bytes) in the TransferRequest response.
                In that case, we rely on the cached, correct file size we received when
                we initially added the download. """
                if msg.filesize > 0:
                    i.size = msg.filesize

                i.req = msg.req
                i.status = "Waiting for download"
                transfertimeout = TransferTimeout(i.req, self.network_callback)

                if i.transfertimer is not None:
                    i.transfertimer.cancel()

                i.transfertimer = threading.Timer(30.0, transfertimeout.timeout)
                i.transfertimer.setDaemon(True)
                i.transfertimer.start()
                response = slskmessages.TransferResponse(conn, 1, req=i.req)
                self.downloadsview.update(i)
                break
        else:
            # If this file is not in your download queue, then it must be
            # a remotely initated download and someone is manually uploading to you
            if self.can_upload(user) and user in self.requested_upload_queue:
                path = ""
                if self.eventprocessor.config.sections["transfers"]["uploadsinsubdirs"]:
                    parentdir = msg.file.split("\\")[-2]
                    path = self.eventprocessor.config.sections["transfers"]["uploaddir"] + os.sep + user + os.sep + parentdir

                transfer = Transfer(
                    user=user, filename=msg.file, path=path,
                    status="Getting status", size=msg.filesize, req=msg.req
                )
                self.downloads.append(transfer)

                if user not in self.eventprocessor.watchedusers:
                    self.queue.put(slskmessages.AddUser(user))

                self.queue.put(slskmessages.GetUserStatus(user))
                response = slskmessages.TransferResponse(conn, 0, reason="Queued", req=transfer.req)
                self.downloadsview.update(transfer)
            else:
                response = slskmessages.TransferResponse(conn, 0, reason="Cancelled", req=msg.req)
                log.add_transfer(_("Denied file request: User %(user)s, %(msg)s"), {
                    'user': user,
                    'msg': str(vars(msg))
                })
        return response

    def transfer_request_uploads(self, msg, user, conn, addr):
        """
        Remote peer is requesting to download a file through
        your Upload queue
        """

        response = self._transfer_request_uploads(msg, user, conn, addr)
        log.add_transfer(_("Upload request: %(req)s Response: %(resp)s"), {
            'req': str(vars(msg)),
            'resp': response
        })
        return response

    def _transfer_request_uploads(self, msg, user, conn, addr):

        # Is user alllowed to download?
        checkuser, reason = self.eventprocessor.check_user(user, addr)
        if not checkuser:
            return slskmessages.TransferResponse(conn, 0, reason=reason, req=msg.req)

        # Do we actually share that file with the world?
        realpath = self.eventprocessor.shares.virtual2real(msg.file)
        if not self.file_is_shared(user, msg.file, realpath):
            return slskmessages.TransferResponse(conn, 0, reason="File not shared", req=msg.req)

        # Is that file already in the queue?
        if self.file_is_upload_queued(user, msg.file):
            return slskmessages.TransferResponse(conn, 0, reason="Queued", req=msg.req)

        # Has user hit queue limit?
        friend = user in [i[0] for i in self.eventprocessor.config.sections["server"]["userlist"]]
        if friend and self.eventprocessor.config.sections["transfers"]["friendsnolimits"]:
            limits = False
        else:
            limits = True

        if limits and self.queue_limit_reached(user):
            uploadslimit = self.eventprocessor.config.sections["transfers"]["queuelimit"]
            return slskmessages.TransferResponse(conn, 0, reason="User limit of %i megabytes exceeded" % (uploadslimit), req=msg.req)

        if limits and self.file_limit_reached(user):
            filelimit = self.eventprocessor.config.sections["transfers"]["filelimit"]
            limitmsg = "User limit of %i files exceeded" % (filelimit)
            return slskmessages.TransferResponse(conn, 0, reason=limitmsg, req=msg.req)

        # All checks passed, user can queue file!
        if self.pluginhandler:
            self.pluginhandler.upload_queued_notification(user, msg.file, realpath)

        # Is user already downloading/negotiating a download?
        if not self.allow_new_uploads() or user in self.get_transferring_users():

            response = slskmessages.TransferResponse(conn, 0, reason="Queued", req=msg.req)
            newupload = Transfer(
                user=user, filename=msg.file, realfilename=realpath,
                path=os.path.dirname(realpath), status="Queued",
                timequeued=time.time(), size=self.get_file_size(realpath),
                place=len(self.uploads)
            )
            self._append_upload(user, msg.file, newupload)
            self.uploadsview.update(newupload)
            self.add_queued(user, realpath)
            return response

        # All checks passed, starting a new upload.
        size = self.get_file_size(realpath)
        response = slskmessages.TransferResponse(conn, 1, req=msg.req, filesize=size)

        transfertimeout = TransferTimeout(msg.req, self.network_callback)
        transferobj = Transfer(
            user=user, realfilename=realpath, filename=msg.file,
            path=os.path.dirname(realpath), status="Waiting for upload",
            req=msg.req, size=size, place=len(self.uploads)
        )

        self._append_upload(user, msg.file, transferobj)
        transferobj.transfertimer = threading.Timer(30.0, transfertimeout.timeout)
        transferobj.transfertimer.setDaemon(True)
        transferobj.transfertimer.start()
        self.uploadsview.update(transferobj)
        return response

    def _append_upload(self, user, filename, transferobj):

        for i in self.uploads:
            if i.user == user and i.filename == filename:
                self.uploads.remove(i)
                self.uploadsview.remove_specific(i, True)

        self.uploads.append(transferobj)

    def file_is_upload_queued(self, user, filename):

        for i in self.uploads:
            if i.user == user and i.filename == filename and i.status in self.PRE_TRANSFER + self.TRANSFER:
                return True

        return False

    def queue_limit_reached(self, user):

        uploadslimit = self.eventprocessor.config.sections["transfers"]["queuelimit"] * 1024 * 1024

        if not uploadslimit:
            return False

        size = sum(i.size for i in self.uploads if i.user == user and i.status == "Queued")

        return size >= uploadslimit

    def file_limit_reached(self, user):

        filelimit = self.eventprocessor.config.sections["transfers"]["filelimit"]

        if not filelimit:
            return False

        numfiles = sum(1 for i in self.uploads if i.user == user and i.status == "Queued")

        return numfiles >= filelimit

    def queue_upload(self, msg):
        """ Peer remotely(?) queued a download (upload here) """

        user = None
        for i in self.peerconns:
            if i.conn is msg.conn.conn:
                user = i.username

        if user is None:
            return

        addr = msg.conn.addr[0]
        realpath = self.eventprocessor.shares.virtual2real(msg.file)

        if not self.file_is_upload_queued(user, msg.file):

            friend = user in [i[0] for i in self.eventprocessor.config.sections["server"]["userlist"]]
            if friend and self.eventprocessor.config.sections["transfers"]["friendsnolimits"]:
                limits = 0
            else:
                limits = 1

            checkuser, reason = self.eventprocessor.check_user(user, addr)

            if not checkuser:
                self.queue.put(
                    slskmessages.QueueFailed(conn=msg.conn.conn, file=msg.file, reason=reason)
                )

            elif limits and self.queue_limit_reached(user):
                uploadslimit = self.eventprocessor.config.sections["transfers"]["queuelimit"]
                limitmsg = "User limit of %i megabytes exceeded" % (uploadslimit)
                self.queue.put(
                    slskmessages.QueueFailed(conn=msg.conn.conn, file=msg.file, reason=limitmsg)
                )

            elif limits and self.file_limit_reached(user):
                filelimit = self.eventprocessor.config.sections["transfers"]["filelimit"]
                limitmsg = "User limit of %i files exceeded" % (filelimit)
                self.queue.put(
                    slskmessages.QueueFailed(conn=msg.conn.conn, file=msg.file, reason=limitmsg)
                )

            elif self.file_is_shared(user, msg.file, realpath):
                newupload = Transfer(
                    user=user, filename=msg.file, realfilename=realpath,
                    path=os.path.dirname(realpath), status="Queued",
                    timequeued=time.time(), size=self.get_file_size(realpath)
                )
                self._append_upload(user, msg.file, newupload)
                self.uploadsview.update(newupload)
                self.add_queued(user, msg.file)

                if self.pluginhandler:
                    self.pluginhandler.upload_queued_notification(user, msg.file, realpath)

            else:
                self.queue.put(
                    slskmessages.QueueFailed(conn=msg.conn.conn, file=msg.file, reason="File not shared")
                )

        log.add_transfer(_("Queued upload request: User %(user)s, %(msg)s"), {
            'user': user,
            'msg': str(vars(msg))
        })

        self.check_upload_queue()

    def upload_queue_notification(self, msg):

        username = None

        for i in self.peerconns:
            if i.conn is msg.conn.conn:
                username = i.username
                break

        if username is None:
            return

        if self.can_upload(username):
            log.add(_("Your buddy, %s, is attempting to upload file(s) to you."), username)
            if username not in self.requested_upload_queue:
                self.requested_upload_queue.append(username)
        else:
            self.queue.put(
                slskmessages.MessageUser(username, _("[Automatic Message] ") + _("You are not allowed to send me files."))
            )
            log.add(_("%s is not allowed to send you file(s), but is attempting to, anyway. Warning Sent."), username)

    def can_upload(self, user):

        transfers = self.eventprocessor.config.sections["transfers"]

        if transfers["remotedownloads"] == 1:

            # Remote Uploads only for users in list
            if transfers["uploadallowed"] == 2:
                # Users in userlist
                if user not in [i[0] for i in self.eventprocessor.config.sections["server"]["userlist"]]:
                    # Not a buddy
                    return False

            if transfers["uploadallowed"] == 0:
                # No One can sent files to you
                return False

            if transfers["uploadallowed"] == 1:
                # Everyone can sent files to you
                return True

            if transfers["uploadallowed"] == 3:
                # Trusted Users
                userlist = [i[0] for i in self.eventprocessor.config.sections["server"]["userlist"]]

                if user not in userlist:
                    # Not a buddy
                    return False
                if not self.eventprocessor.config.sections["server"]["userlist"][userlist.index(user)][4]:
                    # Not Trusted
                    return False

            return True

        return False

    def queue_failed(self, msg):

        for i in self.peerconns:
            if i.conn is msg.conn.conn:
                user = i.username
                break

        for i in self.downloads:
            if i.user == user and i.filename == msg.file and i.status not in ["Aborted", "Paused"]:
                if i.status in self.TRANSFER:
                    self.abort_transfer(i, reason=msg.reason)

                i.status = msg.reason
                self.downloadsview.update(i)

                break

    def file_is_shared(self, user, virtualfilename, realfilename):

        realfilename = realfilename.replace("\\", os.sep)
        if not os.access(realfilename, os.R_OK):
            return False

        (dir, sep, file) = virtualfilename.rpartition('\\')

        if self.eventprocessor.config.sections["transfers"]["enablebuddyshares"]:
            if user in [i[0] for i in self.eventprocessor.config.sections["server"]["userlist"]]:
                bshared = self.eventprocessor.config.sections["transfers"]["bsharedfiles"]
                for i in bshared.get(str(dir), ''):
                    if file == i[0]:
                        return True

        shared = self.eventprocessor.config.sections["transfers"]["sharedfiles"]

        for i in shared.get(str(dir), ''):
            if file == i[0]:
                return True

        return False

    def get_transferring_users(self):
        return [i.user for i in self.uploads if i.req is not None or i.conn is not None or i.status == "Getting status"]  # some file is being transfered

    def transfer_negotiating(self):

        # some file is being negotiated
        now = time.time()
        count = 0

        for i in self.uploads:
            if (now - i.laststatuschange) < 30:  # if a status hasn't changed in the last 30 seconds the connection is probably never going to work, ignoring it.

                if i.req is not None:
                    count += 1
                if i.conn is not None and i.speed is None:
                    count += 1
                if i.status == "Getting status":
                    count += 1

        return count

    def allow_new_uploads(self):

        limit_upload_slots = self.eventprocessor.config.sections["transfers"]["useupslots"]
        limit_upload_speed = self.eventprocessor.config.sections["transfers"]["uselimit"]

        bandwidth_sum = sum(i.speed for i in self.uploads if i.conn is not None and i.speed is not None)
        currently_negotiating = self.transfer_negotiating()

        if limit_upload_slots:
            maxupslots = self.eventprocessor.config.sections["transfers"]["uploadslots"]
            in_progress_count = sum(1 for i in self.uploads if i.conn is not None and i.speed is not None)

            if in_progress_count + currently_negotiating >= maxupslots:
                return False

        if limit_upload_speed:
            max_upload_speed = self.eventprocessor.config.sections["transfers"]["uploadlimit"] * 1024

            if bandwidth_sum >= max_upload_speed:
                return False

            if currently_negotiating:
                return False

        maxbandwidth = self.eventprocessor.config.sections["transfers"]["uploadbandwidth"] * 1024
        if maxbandwidth:
            if bandwidth_sum >= maxbandwidth:
                return False

        return True

    def get_file_size(self, filename):

        try:
            size = os.path.getsize(filename.replace("\\", os.sep))
        except Exception:
            # file doesn't exist (remote files are always this)
            size = 0

        return size

    def transfer_response(self, msg):
        """ Got a response to the file request from the peer."""

        if msg.reason is not None:

            for i in self.downloads:

                if i.req != msg.req:
                    continue

                i.status = msg.reason
                i.req = None
                self.downloadsview.update(i)

                if msg.reason == "Queued":

                    if i.user not in self.users or self.users[i.user].status is None:
                        if i.user not in self.eventprocessor.watchedusers:
                            self.queue.put(slskmessages.AddUser(i.user))
                        self.queue.put(slskmessages.GetUserStatus(i.user))

                    self.eventprocessor.process_request_to_peer(i.user, slskmessages.PlaceInQueueRequest(None, i.filename))

                self.check_upload_queue()
                break

            for i in self.uploads:

                if i.req != msg.req:
                    continue

                i.status = msg.reason
                i.req = None
                self.uploadsview.update(i)

                if msg.reason == "Queued":

                    if i.user not in self.users or self.users[i.user].status is None:
                        if i.user not in self.eventprocessor.watchedusers:
                            self.queue.put(slskmessages.AddUser(i.user))
                        self.queue.put(slskmessages.GetUserStatus(i.user))

                    if i.transfertimer is not None:
                        i.transfertimer.cancel()

                    self.uploads.remove(i)
                    self.uploadsview.remove_specific(i, True)

                elif msg.reason == "Cancelled":

                    self.auto_clear_upload(i)

                self.check_upload_queue()
                break

        elif msg.filesize is not None:
            for i in self.downloads:

                if i.req != msg.req:
                    continue

                i.size = msg.filesize
                i.status = "Establishing connection"
                # Have to establish 'F' connection here
                self.eventprocessor.process_request_to_peer(i.user, slskmessages.FileRequest(None, msg.req))
                self.downloadsview.update(i)
                break
        else:
            for i in self.uploads:

                if i.req != msg.req:
                    continue

                i.status = "Establishing connection"
                self.eventprocessor.process_request_to_peer(i.user, slskmessages.FileRequest(None, msg.req))
                self.uploadsview.update(i)
                self.check_upload_queue()
                break
            else:
                log.add_transfer(_("Got unknown transfer response: %s"), str(vars(msg)))

    def transfer_timeout(self, msg):

        for i in (self.downloads + self.uploads):

            if i.req != msg.req:
                continue

            if i.status in ["Queued", "User logged off", "Paused"] + self.COMPLETED_TRANSFERS:
                continue

            i.status = "Cannot connect"
            i.req = None
            curtime = time.time()

            for j in self.uploads:
                if j.user == i.user:
                    j.timequeued = curtime

            if i.user not in self.eventprocessor.watchedusers:
                self.queue.put(slskmessages.AddUser(i.user))

            self.queue.put(slskmessages.GetUserStatus(i.user))

            if i in self.downloads:
                self.downloadsview.update(i)
            elif i in self.uploads:
                self.uploadsview.update(i)

            break

        self.check_upload_queue()

    def file_request(self, msg):
        """ Got an incoming file request. Could be an upload request or a
        request to get the file that was previously queued"""

        for i in self.downloads:
            if msg.req == i.req:
                self._file_request_download(msg, i)
                return

        for i in self.uploads:
            if msg.req == i.req:
                self._file_request_upload(msg, i)
                return

        self.queue.put(slskmessages.ConnClose(msg.conn))

    def _file_request_download(self, msg, i):

        downloaddir = self.eventprocessor.config.sections["transfers"]["downloaddir"]
        incompletedir = self.eventprocessor.config.sections["transfers"]["incompletedir"]
        needupdate = True

        if i.conn is None and i.size is not None:
            i.conn = msg.conn
            i.req = None

            if i.transfertimer is not None:
                i.transfertimer.cancel()

            if not incompletedir:
                if i.path and i.path[0] == '/':
                    incompletedir = clean_path(i.path)
                else:
                    incompletedir = os.path.join(downloaddir, clean_path(i.path))

            try:
                if not os.access(incompletedir, os.F_OK):
                    os.makedirs(incompletedir)
                if not os.access(incompletedir, os.R_OK | os.W_OK | os.X_OK):
                    raise OSError("Download directory %s Permissions error.\nDir Permissions: %s" % (incompletedir, oct(os.stat(incompletedir)[stat.ST_MODE] & 0o777)))

            except OSError as strerror:
                log.add(_("OS error: %s"), strerror)
                i.status = "Download directory error"
                i.conn = None
                self.queue.put(slskmessages.ConnClose(msg.conn))

                if self.notifications:
                    self.notifications.new_notification(_("OS error: %s") % strerror, title=_("Folder download error"))

            else:
                # also check for a windows-style incomplete transfer
                basename = clean_file(i.filename.split('\\')[-1])
                winfname = os.path.join(incompletedir, "INCOMPLETE~" + basename)
                pyfname = os.path.join(incompletedir, "INCOMPLETE" + basename)

                m = hashlib.md5()
                m.update((i.filename + i.user).encode('utf-8'))

                pynewfname = os.path.join(incompletedir, "INCOMPLETE" + m.hexdigest() + basename)
                try:
                    if os.access(winfname, os.F_OK):
                        fname = winfname
                    elif os.access(pyfname, os.F_OK):
                        fname = pyfname
                    else:
                        fname = pynewfname

                    f = open(fname, 'ab+')

                except IOError as strerror:
                    log.add(_("Download I/O error: %s"), strerror)
                    i.status = "Local file error"
                    try:
                        f.close()
                    except Exception:
                        pass
                    i.conn = None
                    self.queue.put(slskmessages.ConnClose(msg.conn))

                else:
                    if self.eventprocessor.config.sections["transfers"]["lock"]:
                        try:
                            import fcntl
                            try:
                                fcntl.lockf(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                            except IOError as strerror:
                                log.add(_("Can't get an exclusive lock on file - I/O error: %s"), strerror)
                        except ImportError:
                            pass

                    f.seek(0, 2)
                    size = f.tell()

                    i.currentbytes = size
                    i.file = f
                    i.place = 0
                    i.offset = size
                    i.starttime = time.time()

                    if i.size > size:
                        i.status = "Transferring"
                        self.queue.put(slskmessages.DownloadFile(i.conn, size, f, i.size))
                        log.add_transfer(_("Download started: %s"), f.name)

                        self.log_transfer(_("Download started: user %(user)s, file %(file)s") % {'user': i.user, 'file': "%s" % f.name}, show_ui=1)
                    else:
                        self.download_finished(f, i)
                        needupdate = False

            self.downloadsview.new_transfer_notification()

            if needupdate:
                self.downloadsview.update(i)
        else:
            log.add_warning(_("Download error formally known as 'Unknown file request': %(req)s (%(user)s: %(file)s)"), {
                'req': str(vars(msg)),
                'user': i.user,
                'file': i.filename
            })

            self.queue.put(slskmessages.ConnClose(msg.conn))

    def _file_request_upload(self, msg, i):

        if i.conn is None:
            i.conn = msg.conn
            i.req = None

            if i.transfertimer is not None:
                i.transfertimer.cancel()

            try:
                # Open File
                filename = i.realfilename.replace("\\", os.sep)

                f = open(filename, "rb")
                self.queue.put(slskmessages.UploadFile(i.conn, file=f, size=i.size))
                i.status = "Initializing transfer"
                i.file = f

                self.log_transfer(_("Upload started: user %(user)s, file %(file)s") % {
                    'user': i.user,
                    'file': i.filename
                })
            except IOError as strerror:
                log.add(_("Upload I/O error: %s"), strerror)
                i.status = "Local file error"
                try:
                    f.close()
                except Exception:
                    pass
                i.conn = None
                self.queue.put(slskmessages.ConnClose(msg.conn))

            self.uploadsview.new_transfer_notification()
            self.uploadsview.update(i)
        else:
            log.add_warning(_("Upload error formally known as 'Unknown file request': %(req)s (%(user)s: %(file)s)"), {
                'req': str(vars(msg)),
                'user': i.user,
                'file': i.filename
            })

            self.queue.put(slskmessages.ConnClose(msg.conn))

    def file_download(self, msg):
        """ A file download is in progress"""

        needupdate = True

        for i in self.downloads:

            if i.conn != msg.conn:
                continue

            try:

                if i.transfertimer is not None:
                    i.transfertimer.cancel()
                curtime = time.time()

                i.currentbytes = msg.file.tell()

                if i.lastbytes is None:
                    i.lastbytes = i.currentbytes
                if i.starttime is None:
                    i.starttime = curtime
                if i.lasttime is None:
                    i.lasttime = curtime - 1

                i.status = "Transferring"
                oldelapsed = i.timeelapsed
                i.timeelapsed = curtime - i.starttime

                if curtime > i.starttime and \
                        i.currentbytes > i.lastbytes:

                    try:
                        i.speed = max(0, (i.currentbytes - i.lastbytes) / (curtime - i.lasttime))
                    except ZeroDivisionError:
                        i.speed = 0
                    if i.speed <= 0.0:
                        i.timeleft = "∞"
                    else:
                        i.timeleft = self.get_time((i.size - i.currentbytes) / i.speed)

                i.lastbytes = i.currentbytes
                i.lasttime = curtime

                if i.size > i.currentbytes:
                    if oldelapsed == i.timeelapsed:
                        needupdate = False
                else:
                    self.download_finished(msg.file, i)
                    needupdate = False
            except IOError as strerror:
                log.add(_("Download I/O error: %s"), strerror)
                i.status = "Local file error"
                try:
                    msg.file.close()
                except Exception:
                    pass
                i.conn = None
                self.queue.put(slskmessages.ConnClose(msg.conn))

            if needupdate:
                self.downloadsview.update(i)

            break

    def download_finished(self, file, i):
        file.close()
        i.file = None

        basename = clean_file(i.filename.split('\\')[-1])
        config = self.eventprocessor.config.sections
        downloaddir = config["transfers"]["downloaddir"]

        if i.path and i.path[0] == '/':
            folder = clean_path(i.path)
        else:
            folder = os.path.join(downloaddir, i.path)

        if not os.access(folder, os.F_OK):
            os.makedirs(folder)

        newname = self.get_renamed(os.path.join(folder, basename))

        try:
            shutil.move(file.name, newname)
        except (IOError, OSError) as inst:
            log.add_warning(
                _("Couldn't move '%(tempfile)s' to '%(file)s': %(error)s"), {
                    'tempfile': "%s" % file.name,
                    'file': newname,
                    'error': inst
                }
            )

        i.status = "Finished"
        i.speed = 0
        i.timeleft = ""

        log.add_transfer(
            _("Download finished: %(file)s"), {
                'file': newname
            }
        )

        self.log_transfer(
            _("Download finished: user %(user)s, file %(file)s") % {
                'user': i.user,
                'file': i.filename
            },
            show_ui=1
        )

        self.queue.put(slskmessages.ConnClose(i.conn))
        i.conn = None

        self.add_to_shared(newname)
        self.eventprocessor.shares.send_num_shared_folders_files()

        if self.notifications and config["notifications"]["notification_popup_file"]:
            self.notifications.new_notification(
                _("%(file)s downloaded from %(user)s") % {
                    'user': i.user,
                    'file': newname.rsplit(os.sep, 1)[1]
                },
                title=_("File downloaded")
            )

        self.save_downloads()

        # Attempt to autoclear this download, if configured
        if not self.auto_clear_download(i):
            self.downloadsview.update(i)

        if config["transfers"]["afterfinish"]:
            if not execute_command(config["transfers"]["afterfinish"], newname):
                log.add(_("Trouble executing '%s'"), config["transfers"]["afterfinish"])
            else:
                log.add(_("Executed: %s"), config["transfers"]["afterfinish"])

        if i.path and (config["notifications"]["notification_popup_folder"] or config["transfers"]["afterfolder"]):

            # walk through downloads and break if any file in the same folder exists, else execute
            for ia in self.downloads:
                if ia.status not in ["Finished", "Aborted", "Paused", "Filtered"] and ia.path and ia.path == i.path:
                    break
            else:
                if self.notifications and config["notifications"]["notification_popup_folder"]:
                    self.notifications.new_notification(
                        _("%(folder)s downloaded from %(user)s") % {
                            'user': i.user,
                            'folder': folder
                        },
                        title=_("Folder downloaded")
                    )
                if config["transfers"]["afterfolder"]:
                    if not execute_command(config["transfers"]["afterfolder"], folder):
                        log.add(_("Trouble executing on folder: %s"), config["transfers"]["afterfolder"])
                    else:
                        log.add(_("Executed on folder: %s"), config["transfers"]["afterfolder"])

    def add_to_shared(self, name):
        """ Add a file to the normal shares database """

        self.eventprocessor.shares.add_file_to_shared(name)

    def file_upload(self, msg):
        """ A file upload is in progress """

        needupdate = True

        for i in self.uploads:

            if i.conn != msg.conn:
                continue

            if i.transfertimer is not None:
                i.transfertimer.cancel()

            curtime = time.time()
            if i.starttime is None:
                i.starttime = curtime
                i.offset = msg.offset

            lastspeed = 0
            if i.speed is not None:
                lastspeed = i.speed

            i.currentbytes = msg.offset + msg.sentbytes
            oldelapsed = i.timeelapsed
            i.timeelapsed = curtime - i.starttime

            if curtime > i.starttime and \
                    i.currentbytes > i.lastbytes:

                try:
                    i.speed = max(0, (i.currentbytes - i.lastbytes) / (curtime - i.lasttime))
                except ZeroDivisionError:
                    i.speed = lastspeed  # too fast!

                if i.speed <= 0.0 and (i.currentbytes != i.size or lastspeed == 0):
                    i.timeleft = "∞"
                else:
                    if (i.currentbytes == i.size) and i.speed == 0:
                        i.speed = lastspeed
                    i.timeleft = self.get_time((i.size - i.currentbytes) / i.speed)

                self.check_upload_queue()

            i.lastbytes = i.currentbytes
            i.lasttime = curtime

            if i.size > i.currentbytes:
                if oldelapsed == i.timeelapsed:
                    needupdate = False
                i.status = "Transferring"

                if i.user in self.privilegedusers:
                    i.modifier = _("(privileged)")
                elif self.user_list_privileged(i.user):
                    i.modifier = _("(friend)")
            elif i.size is None:
                # Failed?
                self.check_upload_queue()
                sleep(0.01)
            else:
                if i.speed is not None:
                    speedbytes = int(i.speed)
                    self.eventprocessor.speed = speedbytes
                    self.queue.put(slskmessages.SendUploadSpeed(speedbytes))

                msg.file.close()
                i.status = "Finished"
                i.speed = 0
                i.timeleft = ""

                for j in self.uploads:
                    if j.user == i.user:
                        j.timequeued = curtime

                self.log_transfer(
                    _("Upload finished: %(user)s, file %(file)s") % {
                        'user': i.user,
                        'file': i.filename
                    }
                )

                self.check_upload_queue()
                self.uploadsview.update(i)

                # Autoclear this upload
                self.auto_clear_upload(i)
                needupdate = False

            if needupdate:
                self.uploadsview.update(i)

            break

    def auto_clear_download(self, transfer):
        if self.eventprocessor.config.sections["transfers"]["autoclear_downloads"]:
            self.downloads.remove(transfer)
            self.downloadsview.remove_specific(transfer, True)
            return True

        return False

    def auto_clear_upload(self, transfer):
        if self.eventprocessor.config.sections["transfers"]["autoclear_uploads"]:
            self.uploads.remove(transfer)
            self.uploadsview.remove_specific(transfer, True)
            self.calc_upload_queue_sizes()
            self.check_upload_queue()

    def ban_user(self, user, ban_message=None):
        """
        Ban a user, cancel all the user's uploads, send a 'Banned'
        message via the transfers, and clear the transfers from the
        uploads list.
        """

        if ban_message:
            banmsg = _("Banned (%s)") % ban_message
        elif self.eventprocessor.config.sections["transfers"]["usecustomban"]:
            banmsg = _("Banned (%s)") % self.eventprocessor.config.sections["transfers"]["customban"]
        else:
            banmsg = _("Banned")

        for upload in self.uploads:
            if upload.user != user:
                continue

            if upload.status == "Queued":
                self.eventprocessor.process_request_to_peer(user, slskmessages.QueueFailed(None, file=upload.filename, reason=banmsg))
            else:
                self.abort_transfer(upload, reason=banmsg)

        if self.uploadsview is not None:
            self.uploadsview.clear_by_user(user)
        if user not in self.eventprocessor.config.sections["server"]["banlist"]:
            self.eventprocessor.config.sections["server"]["banlist"].append(user)
            self.eventprocessor.config.write_configuration()
            self.eventprocessor.config.write_download_queue()

    def start_check_download_queue_timer(self):
        timer = threading.Timer(60.0, self.check_download_queue)
        timer.setDaemon(True)
        timer.start()

    # Find failed or stuck downloads and attempt to queue them.
    # Also ask for the queue position of downloads.
    def check_download_queue(self):

        statuslist = self.FAILED_TRANSFERS + \
            ["Getting status", "Getting address", "Connecting", "Waiting for peer to connect", "Requesting file", "Initializing transfer"]

        for transfer in self.downloads:
            if transfer.status in statuslist:
                self.abort_transfer(transfer)
                self.get_file(transfer.user, transfer.filename, transfer.path, transfer)
            elif transfer.status == "Queued":
                self.eventprocessor.process_request_to_peer(transfer.user, slskmessages.PlaceInQueueRequest(None, transfer.filename))

        self.start_check_download_queue_timer()

    # Find next file to upload
    def check_upload_queue(self):

        if not self.allow_new_uploads():
            return

        transfercandidate = None
        trusers = self.get_transferring_users()

        # List of transfer instances of users who are not currently transferring
        list_queued = [i for i in self.uploads if i.user not in trusers and i.status == "Queued"]

        # Sublist of privileged users transfers
        list_privileged = [i for i in list_queued if self.is_privileged(i.user)]

        if len(list_privileged) > 0:
            # Upload to a privileged user
            # Only Privileged users' files will get selected
            list_queued = list_privileged

        if len(list_queued) == 0:
            return

        if self.eventprocessor.config.sections["transfers"]["fifoqueue"]:
            # FIFO
            # Get the first item in the list
            transfercandidate = list_queued[0]
        else:
            # Round Robin
            # Get first transfer that was queued less than one second from now
            mintimequeued = time.time() + 1
            for i in list_queued:
                if i.timequeued < mintimequeued:
                    transfercandidate = i
                    # Break loop
                    mintimequeued = i.timequeued

        if transfercandidate is not None:
            self.push_file(
                user=transfercandidate.user, filename=transfercandidate.filename,
                realfilename=transfercandidate.realfilename, transfer=transfercandidate
            )
            self.remove_queued(transfercandidate.user, transfercandidate.filename)

    def place_in_queue_request(self, msg):

        for i in self.peerconns:
            if i.conn is msg.conn.conn:
                user = i.username

        def list_users():
            users = []
            for i in self.uploads:
                if i.user not in users:
                    users.append(i.user)
            return users

        def count_transfers(username):
            transfers = []
            for i in self.uploads:
                if i.status == "Queued":
                    if i.user == username:
                        transfers.append(i)
            return len(transfers)

        if self.eventprocessor.config.sections["transfers"]["fifoqueue"]:

            # Number of transfers queued by non-privileged users
            count = 0

            # Number of transfers queued by privileged users
            countpriv = 0

            # Place in the queue for msg.file
            place = 0

            for i in self.uploads:
                # Ignore non-queued files
                if i.status == "Queued":
                    if self.is_privileged(i.user):
                        countpriv += 1
                    else:
                        count += 1

                    # Stop counting on the matching file
                    if i.user == user and i.filename == msg.file:
                        if self.is_privileged(user):
                            # User is privileged so we only
                            # count priv'd transfers
                            place = countpriv
                        else:
                            # Count all transfers
                            place = count + countpriv
                        break
        else:
            # Todo
            listpriv = {user: time.time()}
            countpriv = 0
            trusers = self.get_transferring_users()
            count = 0
            place = 0
            transfers = 0

            for i in self.uploads:
                # Ignore non-queued files
                if i.status == "Queued":
                    if i.user == user:
                        if self.is_privileged(user):
                            # User is privileged so we only
                            # count priv'd transfers
                            listpriv[i.user] = i.timequeued
                            place += 1
                        else:
                            # Count all transfers
                            place += 1
                        # Stop counting on the matching file
                        if i.filename == msg.file:
                            break

            upload_users = list_users()
            user_transfers = {}

            for username in upload_users:
                user_transfers[username] = count_transfers(username)
                if username is not user:
                    if user_transfers[username] >= place:
                        if username not in trusers:
                            transfers += place

            place += transfers

        self.queue.put(slskmessages.PlaceInQueue(msg.conn.conn, msg.file, place))

    def get_time(self, seconds):

        sec = int(seconds % 60)
        minutes = int(seconds / 60 % 60)
        hours = int(seconds / 3600 % 24)
        days = int(seconds / 86400)

        time_string = "%02d:%02d:%02d" % (hours, minutes, sec)
        if days > 0:
            time_string = str(days) + "." + time_string

        return time_string

    def calc_upload_queue_sizes(self):
        # queue sizes
        self.privcount = 0
        self.usersqueued = {}
        self.privusersqueued = {}

        for i in self.uploads:
            if i.status == "Queued":
                self.add_queued(i.user, i.filename)

    def get_upload_queue_sizes(self, username=None):

        if self.eventprocessor.config.sections["transfers"]["fifoqueue"]:
            count = 0
            for i in self.uploads:
                if i.status == "Queued":
                    count += 1
            return count, count
        else:
            if username is not None and self.is_privileged(username):
                return len(self.privusersqueued), len(self.privusersqueued)
            else:
                return len(self.usersqueued) + self.privcount, self.privcount

    def add_queued(self, user, filename):

        if user in self.privilegedusers:
            self.privusersqueued.setdefault(user, 0)
            self.privusersqueued[user] += 1
            self.privcount += 1
        else:
            self.usersqueued.setdefault(user, 0)
            self.usersqueued[user] += 1

    def remove_queued(self, user, filename):

        if user in self.privilegedusers:
            self.privusersqueued[user] -= 1
            self.privcount -= 1
            if self.privusersqueued[user] == 0:
                del self.privusersqueued[user]
        else:
            self.usersqueued[user] -= 1
            if self.usersqueued[user] == 0:
                del self.usersqueued[user]

    def get_total_uploads_allowed(self):

        useupslots = self.eventprocessor.config.sections["transfers"]["useupslots"]

        if useupslots:
            maxupslots = self.eventprocessor.config.sections["transfers"]["uploadslots"]
            return maxupslots
        else:
            lstlen = sum(1 for i in self.uploads if i.conn is not None)
            if self.allow_new_uploads():
                return lstlen + 1
            else:
                return lstlen

    def user_list_privileged(self, user):

        # All users
        if self.eventprocessor.config.sections["transfers"]["preferfriends"]:
            return any(user in i[0] for i in self.eventprocessor.config.sections["server"]["userlist"])

        # Only privileged users
        userlist = [i[0] for i in self.eventprocessor.config.sections["server"]["userlist"]]
        if user not in userlist:
            return False

        if self.eventprocessor.config.sections["server"]["userlist"][userlist.index(user)][3]:
            return True
        else:
            return False

    def is_privileged(self, user):

        if user in self.privilegedusers or self.user_list_privileged(user):
            return True
        else:
            return False

    def conn_close(self, conn, addr, user, error):
        """ The remote user has closed the connection either because
        he logged off, or because there's a network problem. """

        for i in self.downloads:
            if i.conn != conn:
                continue

            self._conn_close(conn, addr, i, "download")

        for i in self.uploads:
            if not isinstance(error, ConnectionRefusedError) and i.conn != conn:
                continue
            if i.user != user:
                # Connection refused, cancel all of user's transfers
                continue

            self._conn_close(conn, addr, i, "upload")

    def _conn_close(self, conn, addr, i, type):
        if i.requestconn == conn and i.status == "Requesting file":
            i.requestconn = None
            i.status = "Connection closed by peer"
            i.req = None

            if type == "download":
                self.downloadsview.update(i)
            elif type == "upload":
                self.uploadsview.update(i)

            self.check_upload_queue()

        if i.file is not None:
            i.file.close()

        if i.status != "Finished":
            if i.user in self.users and self.users[i.user].status == 0:
                i.status = "User logged off"
            elif type == "download":
                i.status = "Connection closed by peer"
            elif type == "upload":
                i.status = "Cancelled"
                self.abort_transfer(i)
                self.auto_clear_upload(i)

        curtime = time.time()
        for j in self.uploads:
            if j.user == i.user:
                j.timequeued = curtime

        i.conn = None

        if type == "download":
            self.downloadsview.update(i)
        elif type == "upload":
            self.uploadsview.update(i)

        self.check_upload_queue()

    def get_renamed(self, name):
        """ When a transfer is finished, we remove INCOMPLETE~ or INCOMPLETE
        prefix from the file's name.

        Checks if a file with the same name already exists, and adds a number
        to the file name if that's the case. """

        filename, extension = os.path.splitext(name)
        counter = 1

        while os.path.exists(name):
            name = filename + " (" + str(counter) + ")" + extension
            counter += 1

        return name

    def place_in_queue(self, msg):
        """ The server tells us our place in queue for a particular transfer."""

        username = None
        for i in self.peerconns:
            if i.conn is msg.conn.conn:
                username = i.username
                break

        if username:
            for i in self.downloads:
                if i.user != username:
                    continue

                if i.filename != msg.filename:
                    continue

                i.place = msg.place
                self.downloadsview.update(i)
                break

    def file_error(self, msg):
        """ Networking thread encountered a local file error"""

        for i in self.downloads + self.uploads:

            if i.conn != msg.conn.conn:
                continue
            i.status = "Local file error"

            try:
                msg.file.close()
            except Exception:
                pass

            i.conn = None
            self.queue.put(slskmessages.ConnClose(msg.conn.conn))
            log.add(_("I/O error: %s"), msg.strerror)

            if i in self.downloads:
                self.downloadsview.update(i)
            elif i in self.uploads:
                self.uploadsview.update(i)

            self.check_upload_queue()

    def folder_contents_response(self, conn, file_list):
        """ When we got a contents of a folder, get all the files in it, but
        skip the files in subfolders"""

        username = None
        for i in self.peerconns:
            if i.conn is conn:
                username = i.username
                break

        if username is None:
            return

        for i in file_list:
            for directory in file_list[i]:

                if os.path.commonprefix([i, directory]) == directory:
                    priorityfiles = []
                    normalfiles = []

                    if self.eventprocessor.config.sections["transfers"]["prioritize"]:
                        for file in file_list[i][directory]:
                            parts = file[1].rsplit('.', 1)
                            if len(parts) == 2 and parts[1] in ['sfv', 'md5', 'nfo']:
                                priorityfiles.append(file)
                            else:
                                normalfiles.append(file)
                    else:
                        normalfiles = file_list[i][directory][:]

                    if self.eventprocessor.config.sections["transfers"]["reverseorder"]:
                        deco = [(x[1], x) for x in normalfiles]
                        deco.sort(reverse=True)
                        normalfiles = [x for junk, x in deco]

                    for file in priorityfiles + normalfiles:
                        size = file[2]
                        h_bitrate, bitrate, h_length = get_result_bitrate_length(size, file[4])

                        if directory[-1] == '\\':
                            self.get_file(
                                username,
                                directory + file[1],
                                self.folder_destination(username, directory),
                                size=size,
                                bitrate=h_bitrate,
                                length=h_length,
                                checkduplicate=True
                            )
                        else:
                            self.get_file(
                                username,
                                directory + '\\' + file[1],
                                self.folder_destination(username, directory),
                                size=size,
                                bitrate=h_bitrate,
                                length=h_length,
                                checkduplicate=True
                            )

    def folder_destination(self, user, directory):

        destination = ""

        if user in self.eventprocessor.requested_folders:
            if directory in self.eventprocessor.requested_folders[user]:
                destination += self.eventprocessor.requested_folders[user][directory]

        if directory[-1] == '\\':
            parent = directory.split('\\')[-2]
        else:
            parent = directory.split('\\')[-1]

        destination = os.path.join(destination, parent)

        if destination[0] != '/':
            destination = os.path.join(
                self.eventprocessor.config.sections["transfers"]["downloaddir"],
                destination
            )

        """ Make sure the target folder doesn't exist
        If it exists, append a number to the folder name """

        orig_destination = destination
        counter = 1

        while os.path.exists(destination):
            destination = orig_destination + " (" + str(counter) + ")"
            counter += 1

        return destination

    def abort_transfers(self):
        """ Stop all transfers """

        for i in self.downloads + self.uploads:
            if i.status in ("Aborted", "Paused"):
                self.abort_transfer(i)
                i.status = "Paused"
            elif i.status != "Finished":
                self.abort_transfer(i)
                i.status = "Old"

    def abort_transfer(self, transfer, remove=0, reason="Aborted"):

        transfer.req = None
        transfer.speed = 0
        transfer.timeleft = ""

        if transfer in self.uploads:
            self.eventprocessor.process_request_to_peer(transfer.user, slskmessages.QueueFailed(None, file=transfer.filename, reason=reason))

        if transfer.conn is not None:
            self.queue.put(slskmessages.ConnClose(transfer.conn))
            transfer.conn = None

        if transfer.transfertimer is not None:
            transfer.transfertimer.cancel()

        if transfer.file is not None:
            try:
                transfer.file.close()
                if remove:
                    os.remove(transfer.file.name)
            except Exception:
                pass

            transfer.file = None

            if transfer in self.uploads:
                self.log_transfer(
                    _("Upload aborted, user %(user)s file %(file)s") % {
                        'user': transfer.user,
                        'file': transfer.filename
                    }
                )
            else:
                self.log_transfer(
                    _("Download aborted, user %(user)s file %(file)s") % {
                        'user': transfer.user,
                        'file': transfer.filename
                    },
                    show_ui=1
                )

    def log_transfer(self, message, show_ui=0):

        if self.eventprocessor.config.sections["logging"]["transfers"]:
            timestamp_format = self.eventprocessor.config.sections["logging"]["log_timestamp"]
            write_log(self.eventprocessor.config.sections["logging"]["transferslogsdir"], "transfers", message, timestamp_format)

        if show_ui:
            log.add(message)

    def get_downloads(self):
        """ Get a list of incomplete and not aborted downloads """
        return [[i.user, i.filename, i.path, i.status, i.size, i.currentbytes, i.bitrate, i.length] for i in self.downloads if i.status != "Finished"]

    def save_downloads(self):
        """ Save list of files to be downloaded """
        self.eventprocessor.config.sections["transfers"]["downloads"] = self.get_downloads()
        self.eventprocessor.config.write_download_queue()
