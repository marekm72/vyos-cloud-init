# Copyright (C) 2011 Canonical Ltd.
# Copyright (C) 2012 Hewlett-Packard Development Company, L.P.
#
# Author: Scott Moser <scott.moser@canonical.com>
# Author: Juerg Haefliger <juerg.haefliger@hp.com>
#
# This file is part of cloud-init. See LICENSE file for license information.

"""Resizefs: cloud-config module which resizes the filesystem"""

import errno
import os
import stat
from textwrap import dedent

from cloudinit.config.schema import (
    get_schema_doc, validate_cloudconfig_schema)
from cloudinit.settings import PER_ALWAYS
from cloudinit import subp
from cloudinit import util

NOBLOCK = "noblock"
RESIZEFS_LIST_DEFAULT = ['/']

frequency = PER_ALWAYS
distros = ['all']

# Renamed to schema_vyos to pass build tests without modifying upstream sources
schema_vyos = {
    'id': 'cc_resizefs_vyos',
    'name': 'Resizefs',
    'title': 'Resize filesystem',
    'description': dedent("""\
        Resize filesystems to use all avaliable space on partition. This
        module is useful along with ``cc_growpart`` and will ensure that if a
        partition has been resized the filesystem will be resized
        along with it. By default, ``cc_resizefs`` will resize the root
        partition and will block the boot process while the resize command is
        running. Optionally, the resize operation can be performed in the
        background while cloud-init continues running modules. This can be
        enabled by setting ``resizefs_enabled`` to ``noblock``. This module can
        be disabled altogether by setting ``resizefs_enabled`` to ``false``.
        """),
    'distros': distros,
    'examples': [
        'resizefs_enabled: false  # disable filesystems resize operation'
        'resize_fs: ["/", "/dev/vda1"]'],
    'frequency': PER_ALWAYS,
    'type': 'object',
    'properties': {
        'resizefs_enabled': {
            'enum': [True, False, NOBLOCK],
            'description': dedent("""\
                Whether to resize the partitions. Default: 'true'""")
        },
        'resizefs_list': {
            'type': 'array',
            'items': {'type': 'string'},
            'additionalItems': False,  # Reject items non-string
            'description': dedent("""\
                List of partitions filesystems on which should be resized.
                Default: '/'""")
        }
    }
}

# Renamed to schema_vyos to pass build tests without modifying upstream sources
__doc__ = get_schema_doc(schema_vyos)  # Supplement python help()


def _resize_btrfs(mount_point, devpth):
    # If "/" is ro resize will fail. However it should be allowed since resize
    # makes everything bigger and subvolumes that are not ro will benefit.
    # Use a subvolume that is not ro to trick the resize operation to do the
    # "right" thing. The use of ".snapshot" is specific to "snapper" a generic
    # solution would be walk the subvolumes and find a rw mounted subvolume.
    if (not util.mount_is_read_write(mount_point) and
            os.path.isdir("%s/.snapshots" % mount_point)):
        return ('btrfs', 'filesystem', 'resize', 'max',
                '%s/.snapshots' % mount_point)
    else:
        return ('btrfs', 'filesystem', 'resize', 'max', mount_point)


def _resize_ext(mount_point, devpth):
    return ('resize2fs', devpth)


def _resize_xfs(mount_point, devpth):
    return ('xfs_growfs', mount_point)


def _resize_ufs(mount_point, devpth):
    return ('growfs', '-y', mount_point)


def _resize_zfs(mount_point, devpth):
    return ('zpool', 'online', '-e', mount_point, devpth)


def _can_skip_resize_ufs(mount_point, devpth):
    # possible errors cases on the code-path to growfs -N following:
    # https://github.com/freebsd/freebsd/blob/HEAD/sbin/growfs/growfs.c
    # This is the "good" error:
    skip_start = "growfs: requested size"
    skip_contain = "is not larger than the current filesystem size"
    # growfs exits with 1 for almost all cases up to this one.
    # This means we can't just use rcs=[0, 1] as subp parameter:
    try:
        subp.subp(['growfs', '-N', devpth])
    except subp.ProcessExecutionError as e:
        if e.stderr.startswith(skip_start) and skip_contain in e.stderr:
            # This FS is already at the desired size
            return True
        else:
            raise e
    return False


# Do not use a dictionary as these commands should be able to be used
# for multiple filesystem types if possible, e.g. one command for
# ext2, ext3 and ext4.
RESIZE_FS_PREFIXES_CMDS = [
    ('btrfs', _resize_btrfs),
    ('ext', _resize_ext),
    ('xfs', _resize_xfs),
    ('ufs', _resize_ufs),
    ('zfs', _resize_zfs),
]

RESIZE_FS_PRECHECK_CMDS = {
    'ufs': _can_skip_resize_ufs
}


def can_skip_resize(fs_type, resize_item, devpth):
    fstype_lc = fs_type.lower()
    for i, func in RESIZE_FS_PRECHECK_CMDS.items():
        if fstype_lc.startswith(i):
            return func(resize_item, devpth)
    return False


def maybe_get_writable_device_path(devpath, info, log):
    """Return updated devpath if the devpath is a writable block device.

    @param devpath: Requested path to the root device we want to resize.
    @param info: String representing information about the requested device.
    @param log: Logger to which logs will be added upon error.

    @returns devpath or updated devpath per kernel commandline if the device
        path is a writable block device, returns None otherwise.
    """
    container = util.is_container()

    # Ensure the path is a block device.
    if (devpath == "/dev/root" and not os.path.exists(devpath) and
            not container):
        devpath = util.rootdev_from_cmdline(util.get_cmdline())
        if devpath is None:
            log.warning("Unable to find device '/dev/root'")
            return None
        log.debug("Converted /dev/root to '%s' per kernel cmdline", devpath)

    if devpath == 'overlayroot':
        log.debug("Not attempting to resize devpath '%s': %s", devpath, info)
        return None

    # FreeBSD zpool can also just use gpt/<label>
    # with that in mind we can not do an os.stat on "gpt/whatever"
    # therefore return the devpath already here.
    if devpath.startswith('gpt/'):
        log.debug('We have a gpt label - just go ahead')
        return devpath
    # Alternatively, our device could simply be a name as returned by gpart,
    # such as da0p3
    if not devpath.startswith('/dev/') and not os.path.exists(devpath):
        fulldevpath = '/dev/' + devpath.lstrip('/')
        log.debug("'%s' doesn't appear to be a valid device path. Trying '%s'",
                  devpath, fulldevpath)
        devpath = fulldevpath

    try:
        statret = os.stat(devpath)
    except OSError as exc:
        if container and exc.errno == errno.ENOENT:
            log.debug("Device '%s' did not exist in container. "
                      "cannot resize: %s", devpath, info)
        elif exc.errno == errno.ENOENT:
            log.warning("Device '%s' did not exist. cannot resize: %s",
                        devpath, info)
        else:
            raise exc
        return None

    if not stat.S_ISBLK(statret.st_mode) and not stat.S_ISCHR(statret.st_mode):
        if container:
            log.debug("device '%s' not a block device in container."
                      " cannot resize: %s" % (devpath, info))
        else:
            log.warning("device '%s' not a block device. cannot resize: %s" %
                        (devpath, info))
        return None
    return devpath  # The writable block devpath


def handle(name, cfg, _cloud, log, args):
    if len(args) != 0:
        resize_enabled = args[0]
    else:
        resize_enabled = util.get_cfg_option_str(cfg, "resizefs_enabled", True)

        # Warn about the old-style configuration
        resize_rootfs_option = util.get_cfg_option_str(cfg, "resize_rootfs")
        if resize_rootfs_option:
            log.warning("""The resize_rootfs option is deprecated, please use
                        resizefs_enabled instead!""")
            resize_enabled = resize_rootfs_option

    # Renamed to schema_vyos to pass build tests without modifying upstream
    validate_cloudconfig_schema(cfg, schema_vyos)
    if not util.translate_bool(resize_enabled, addons=[NOBLOCK]):
        log.debug("Skipping module named %s, resizing disabled", name)
        return

    # Get list of partitions to resize
    resize_what = util.get_cfg_option_list(cfg, "resizefs_list",
                                           RESIZEFS_LIST_DEFAULT)
    log.debug("Filesystems to resize: %s", resize_what)

    # Resize all filesystems from resize_what
    for resize_item in resize_what:

        result = util.get_mount_info(resize_item, log)
        if not result:
            log.warning("Could not determine filesystem type of %s",
                        resize_item)
            return

        (devpth, fs_type, mount_point) = result

        # if we have a zfs then our device path at this point
        # is the zfs label. For example: vmzroot/ROOT/freebsd
        # we will have to get the zpool name out of this
        # and set the resize_item variable to the zpool
        # so the _resize_zfs function gets the right attribute.
        if fs_type == 'zfs':
            zpool = devpth.split('/')[0]
            devpth = util.get_device_info_from_zpool(zpool)
            if not devpth:
                return  # could not find device from zpool
            resize_item = zpool

        info = "dev=%s mnt_point=%s path=%s" % (devpth, mount_point,
                                                resize_item)
        log.debug("resize_info: %s" % info)

        devpth = maybe_get_writable_device_path(devpth, info, log)
        if not devpth:
            return  # devpath was not a writable block device

        resizer = None
        if can_skip_resize(fs_type, resize_item, devpth):
            log.debug("Skip resize filesystem type %s for %s",
                      fs_type, resize_item)
            return

        fstype_lc = fs_type.lower()
        for (pfix, root_cmd) in RESIZE_FS_PREFIXES_CMDS:
            if fstype_lc.startswith(pfix):
                resizer = root_cmd
                break

        if not resizer:
            log.warning("Not resizing unknown filesystem type %s for %s",
                        fs_type, resize_item)
            return

        resize_cmd = resizer(resize_item, devpth)
        log.debug("Resizing %s (%s) using %s", resize_item, fs_type,
                  ' '.join(resize_cmd))

        if resize_enabled == NOBLOCK:
            # Fork to a child that will run
            # the resize command
            util.fork_cb(
                util.log_time, logfunc=log.debug, msg="backgrounded Resizing",
                func=do_resize, args=(resize_cmd, log))
        else:
            util.log_time(logfunc=log.debug, msg="Resizing",
                          func=do_resize, args=(resize_cmd, log))

        action = 'Resized'
        if resize_enabled == NOBLOCK:
            action = 'Resizing (via forking)'
        log.debug("%s filesystem on %s (type=%s, val=%s)", action, resize_item,
                  fs_type, resize_enabled)


def do_resize(resize_cmd, log):
    try:
        subp.subp(resize_cmd)
    except subp.ProcessExecutionError:
        util.logexc(log, "Failed to resize filesystem (cmd=%s)", resize_cmd)
        raise
    # TODO(harlowja): Should we add a fsck check after this to make
    # sure we didn't corrupt anything?

# vi: ts=4 expandtab