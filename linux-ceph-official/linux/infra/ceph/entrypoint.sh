#!/usr/bin/env bash
# Brings up a single-node Ceph cluster from the official quay.io/ceph/ceph
# image, which ships the daemons and nothing else: its Cmd is /bin/bash and it
# has no entrypoint, so every step cephadm would normally perform is done here
# instead. One script serves all four daemons; the role comes in as $1 and the
# compose file gives each service its own.
#
#   mon   writes the cluster config and keyrings on first start, then runs
#         ceph-mon. On later starts it only refreshes the monitor address.
#   mgr   registers itself and runs ceph-mgr.
#   osd   creates one BlueStore OSD backed by a file on first start, then runs
#         ceph-osd.
#   rgw   registers itself and runs radosgw on RGW_PORT.
#
# /etc/ceph and /var/lib/ceph are shared volumes, so all four see the same
# config and keyrings. The S3 user is not created here -- the driver scripts
# create it after the stack reports healthy, exactly as they did for the
# all-in-one image.

set -eu

CLUSTER=ceph
MON_NAME=a
CONF="/etc/ceph/${CLUSTER}.conf"
ADMIN_KEYRING="/etc/ceph/${CLUSTER}.client.admin.keyring"
MON_KEYRING="/var/lib/ceph/tmp/${CLUSTER}.mon.keyring"
FSID_FILE="/var/lib/ceph/fsid"
MONMAP="/var/lib/ceph/tmp/monmap"

: "${OSD_SIZE:=40G}"
: "${POOL_SIZE:=1}"
: "${RGW_PORT:=8000}"

# To stderr, not stdout: require_ip runs inside a command substitution, and a
# message written to stdout there would be captured as the address instead of
# being logged.
log () {
  echo "$(date -u '+%Y-%m-%dT%H:%M:%SZ')  ceph-$ROLE: $*" >&2
}

# The container's address on thesis-net. A monitor stores its address in the
# monmap, and Docker hands out a different one every time the container is
# recreated, so this is re-read on every start rather than written once.
#
# The official image carries no iproute2, so there is no `ip` to ask. Both
# routes below use something the image does ship, and the answer is checked
# by the caller -- an empty address here produced
# "monmaptool: invalid ip:port ''" and a monitor that exited 255.
my_ip () {
  local found
  found=$(hostname -i 2>/dev/null | tr ' ' '\n' |
    grep -E '^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$' | head -1)
  if [ -z "$found" ]; then
    found=$(python3 -c 'import socket
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
try:
    s.connect(("192.0.2.1", 9))
    print(s.getsockname()[0])
finally:
    s.close()' 2>/dev/null)
  fi
  echo "$found"
}

# Nothing downstream can recover from an address that was not found, and the
# failures it causes look like Ceph problems rather than a missing tool.
require_ip () {
  local value
  value=$(my_ip)
  if [ -z "$value" ]; then
    log "could not determine this container's IPv4 address on thesis-net"
    log "tried 'hostname -i' and a python3 socket; neither returned one"
    exit 1
  fi
  echo "$value"
}

# Converts 40G / 512M / plain bytes into bytes.
to_bytes () {
  case "$1" in
    *G|*g) echo $(( ${1%[Gg]} * 1024 * 1024 * 1024 )) ;;
    *M|*m) echo $(( ${1%[Mm]} * 1024 * 1024 )) ;;
    *)     echo "$1" ;;
  esac
}

# mgr, osd and rgw all need the monitor answering before they can register.
# The config file may still name the previous monitor address when they start,
# so this retries rather than failing on the first attempt.
wait_for_mon () {
  for _ in $(seq 1 60); do
    if ceph --connect-timeout 5 -s >/dev/null 2>&1; then
      return 0
    fi
    sleep 5
  done
  log "monitor did not answer within 5 minutes"
  return 1
}

bootstrap_cluster () {
  local fsid ip
  ip=$(require_ip)
  fsid=$(uuidgen)
  echo "$fsid" > "$FSID_FILE"
  log "bootstrapping cluster $fsid at $ip"

  # osd crush chooseleaf type = 0 lets CRUSH place replicas on OSDs rather
  # than hosts, which is what makes a single-node cluster reach HEALTH_OK.
  # Pool size defaults to 1 because there is one OSD; with the usual default
  # of 3 every pool would sit undersized and writes would block.
  #
  # Nothing here tunes osd_memory_target. The point of this stack is to
  # measure Ceph's own defaults, so the only settings present are the ones
  # without which a one-node cluster does not come up at all.
  cat > "$CONF" <<ENDCONF
[global]
fsid = $fsid
mon initial members = ${MON_NAME}
mon host = v2:${ip}:3300/0,v1:${ip}:6789/0
auth cluster required = cephx
auth service required = cephx
auth client required = cephx
auth_allow_insecure_global_id_reclaim = false
osd crush chooseleaf type = 0
osd pool default size = ${POOL_SIZE}
osd pool default min size = 1
osd objectstore = bluestore
bluestore block size = $(to_bytes "$OSD_SIZE")
mon allow pool delete = true
mon data avail warn = 5
log to file = false
log to stderr = true

[client.rgw.${MON_NAME}]
rgw frontends = beast port=${RGW_PORT}
rgw thread pool size = 128
ENDCONF

  ceph-authtool --create-keyring "$MON_KEYRING" \
    --gen-key -n mon. --cap mon 'allow *'
  ceph-authtool --create-keyring "$ADMIN_KEYRING" \
    --gen-key -n client.admin \
    --cap mon 'allow *' --cap osd 'allow *' \
    --cap mds 'allow *' --cap mgr 'allow *'
  ceph-authtool "$MON_KEYRING" --import-keyring "$ADMIN_KEYRING"

  # --addv with both addresses, not --add: a plain --add registers the v1
  # address only, which leaves the cluster on the legacy messenger and
  # reports "1 monitors have not enabled msgr2". A default Ceph deployment
  # speaks msgr2, so measuring against v1 would not be measuring the default.
  monmaptool --create \
    --addv "$MON_NAME" "[v2:${ip}:3300,v1:${ip}:6789]" \
    --fsid "$fsid" "$MONMAP"

  mkdir -p "/var/lib/ceph/mon/${CLUSTER}-${MON_NAME}"
  chown -R ceph:ceph /var/lib/ceph /etc/ceph
  sudo -u ceph ceph-mon --mkfs -i "$MON_NAME" \
    --monmap "$MONMAP" --keyring "$MON_KEYRING"
  log "monitor store created"
}

# Docker gives the container a new address on every recreate, which leaves the
# stored monmap pointing at an address nothing answers on. Rewriting both the
# config and the monmap before the daemon starts is safe here because the
# cluster has exactly one monitor, so there is no quorum to preserve.
refresh_mon_address () {
  local ip fsid
  ip=$(require_ip)
  fsid=$(cat "$FSID_FILE")
  if grep -q "v2:${ip}:3300" "$CONF"; then
    log "monitor address unchanged at $ip"
    return 0
  fi
  log "monitor address changed to $ip, rewriting monmap"
  sed -i "s|^mon host = .*|mon host = v2:${ip}:3300/0,v1:${ip}:6789/0|" "$CONF"
  monmaptool --create --clobber \
    --addv "$MON_NAME" "[v2:${ip}:3300,v1:${ip}:6789]" \
    --fsid "$fsid" "$MONMAP"
  chown ceph:ceph "$MONMAP"
  sudo -u ceph ceph-mon -i "$MON_NAME" --inject-monmap "$MONMAP"
}

run_mon () {
  mkdir -p /var/lib/ceph/tmp
  if [ ! -f "$FSID_FILE" ]; then
    bootstrap_cluster
  else
    refresh_mon_address
  fi
  chown -R ceph:ceph /var/lib/ceph /etc/ceph
  log "starting ceph-mon"
  exec ceph-mon -f -i "$MON_NAME" --setuser ceph --setgroup ceph
}

run_mgr () {
  wait_for_mon
  local dir="/var/lib/ceph/mgr/${CLUSTER}-${MON_NAME}"
  if [ ! -f "$dir/keyring" ]; then
    log "registering mgr.${MON_NAME}"
    mkdir -p "$dir"
    ceph auth get-or-create "mgr.${MON_NAME}" \
      mon 'allow profile mgr' osd 'allow *' mds 'allow *' \
      -o "$dir/keyring"
  fi
  chown -R ceph:ceph "$dir"
  log "starting ceph-mgr"
  exec ceph-mgr -f -i "$MON_NAME" --setuser ceph --setgroup ceph
}

run_osd () {
  wait_for_mon
  local dir="/var/lib/ceph/osd/${CLUSTER}-0"
  if [ ! -f "$dir/keyring" ]; then
    local uuid id
    uuid=$(uuidgen)
    id=$(ceph osd new "$uuid")
    log "created osd.$id ($uuid), BlueStore on a ${OSD_SIZE} file"
    mkdir -p "$dir"
    ceph auth get-or-create "osd.$id" \
      mon 'allow profile osd' mgr 'allow profile osd' osd 'allow *' \
      -o "$dir/keyring"
    chown -R ceph:ceph "$dir"
    sudo -u ceph ceph-osd -i "$id" --mkfs --osd-uuid "$uuid"
    # A one-OSD cluster cannot satisfy the default three replicas, so the
    # pools Ceph creates on its own are brought down to POOL_SIZE too.
    ceph config set global osd_pool_default_size "$POOL_SIZE" || true
  fi
  chown -R ceph:ceph "$dir"
  log "starting ceph-osd"
  exec ceph-osd -f -i 0 --setuser ceph --setgroup ceph
}

run_rgw () {
  wait_for_mon
  local dir="/var/lib/ceph/radosgw/${CLUSTER}-rgw.${MON_NAME}"
  if [ ! -f "$dir/keyring" ]; then
    log "registering client.rgw.${MON_NAME}"
    mkdir -p "$dir"
    ceph auth get-or-create "client.rgw.${MON_NAME}" \
      mon 'allow rw' osd 'allow rwx' mgr 'allow rw' \
      -o "$dir/keyring"
  fi
  chown -R ceph:ceph "$dir"
  # The OSD has to be accepting writes before RGW creates its own pools,
  # otherwise the daemon starts and then fails its first request.
  for _ in $(seq 1 60); do
    if ceph --connect-timeout 5 osd stat 2>/dev/null | grep -qE '[1-9][0-9]* up'; then
      break
    fi
    sleep 5
  done
  log "starting radosgw on port ${RGW_PORT}"
  exec radosgw -f -n "client.rgw.${MON_NAME}" --setuser ceph --setgroup ceph
}

ROLE=${1:?usage: entrypoint.sh mon|mgr|osd|rgw}
case "$ROLE" in
  mon) run_mon ;;
  mgr) run_mgr ;;
  osd) run_osd ;;
  rgw) run_rgw ;;
  *)   echo "unknown role: $ROLE" >&2; exit 1 ;;
esac
