#!/bin/bash
# ci-burst router: source NAT for worker subnets that have no other internet path.
#
# This is an OPTIONAL part of a deployment. A worker needs to reach the SSM
# endpoint and the Forgejo instance over HTTPS. When the worker subnets route
# through a NAT gateway or another managed path, nothing here is needed. When
# they are private with no internet path, this instance becomes the default
# route target for those subnets and rewrites their source address, because a
# packet that keeps a private source address is dropped by both the internet
# gateway and the far end of a tunnel.
#
# The artefact deliberately contains no deployment value and no secret. The
# worker CIDRs and the optional WireGuard block are read from ONE SSM
# SecureString at first boot, so the bytes below are identical in every
# deployment and `ec2:DescribeInstanceAttribute` reveals no key:
#
#   {
#     "worker_cidrs": ["192.0.2.0/24", "198.51.100.0/24"],
#     "wireguard_config": "[Interface]\nPrivateKey = ...\n[Peer]\n..."
#   }
#
# `wireguard_config` is optional. Write it to the parameter, not to this file.
#
# The forwarding rules are re-applied by a systemd unit on every boot. A
# first-boot-only `iptables` call would leave the fleet without egress after a
# reboot, which is a worse failure than the one this component exists to fix.
set -euo pipefail
umask 077
exec > >(tee -a /dev/ttyS0) 2>&1 || true

CONFIG_PARAMETER="${CI_BURST_ROUTER_CONFIG_PARAMETER:-/ci-burst/router/config}"
CONFIG_DIR=/etc/ci-burst-router
CIDR_FILE="${CONFIG_DIR}/worker-cidrs"
WG_FILE=/etc/wireguard/wg0.conf
FORWARD=/usr/local/sbin/ci-burst-router-forward
UNIT=/etc/systemd/system/ci-burst-router-forward.service

echo "STEP packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y --no-install-recommends awscli curl wireguard-tools iptables python3

echo "STEP config"
install -d -m 0755 "${CONFIG_DIR}" /etc/wireguard /usr/local/sbin
# The instance profile supplies credentials, not a Region. Resolve it from
# IMDSv2 so the first boot works on an image with no AWS configuration.
TOKEN=$(curl -sS --max-time 5 -X PUT "http://169.254.169.254/latest/api/token" \
  -H "X-aws-ec2-metadata-token-ttl-seconds: 300") || TOKEN=""
REGION=$(curl -sS --max-time 5 -H "X-aws-ec2-metadata-token: ${TOKEN}" \
  "http://169.254.169.254/latest/meta-data/placement/region") || REGION=""
[ -n "${REGION}" ] || { echo "cannot resolve the instance region from IMDS" >&2; exit 1; }
echo "region ${REGION}"
CONFIG_JSON=$(mktemp /run/ci-burst-router-config.XXXXXX)
chmod 600 "${CONFIG_JSON}"
trap 'shred -u "${CONFIG_JSON}" 2>/dev/null || rm -f "${CONFIG_JSON}"' EXIT
aws ssm get-parameter --region "${REGION}" --name "${CONFIG_PARAMETER}" --with-decryption \
  --query Parameter.Value --output text > "${CONFIG_JSON}"

python3 - "${CONFIG_JSON}" "${CIDR_FILE}" "${WG_FILE}" <<'PY'
import ipaddress
import json
import pathlib
import sys

config_path, cidr_path, wg_path = (pathlib.Path(argument) for argument in sys.argv[1:])
payload = json.loads(config_path.read_text())

cidrs = payload.get("worker_cidrs")
if not isinstance(cidrs, list) or not cidrs:
    raise SystemExit("worker_cidrs must be a non-empty list")
networks = []
for value in cidrs:
    if not isinstance(value, str):
        raise SystemExit("every worker_cidrs entry must be a string")
    network = ipaddress.ip_network(value, strict=True)
    if network.version != 4:
        raise SystemExit("worker_cidrs supports IPv4 only")
    networks.append(str(network))
cidr_path.write_text("\n".join(networks) + "\n")
cidr_path.chmod(0o644)

wireguard = payload.get("wireguard_config")
if wireguard is not None:
    if not isinstance(wireguard, str) or not wireguard.strip():
        raise SystemExit("wireguard_config must be a non-empty string when present")
    wg_path.write_text(wireguard if wireguard.endswith("\n") else wireguard + "\n")
    wg_path.chmod(0o600)
print(f"worker CIDRs: {' '.join(networks)}")
print(f"wireguard config: {'present' if wireguard else 'absent'}")
PY

echo "STEP forward"
cat > "${FORWARD}" <<'FORWARD'
#!/bin/bash
# Re-apply the ci-burst router rules for every configured worker CIDR.
# Idempotent: an existing rule is detected with -C before -A.
set -euo pipefail
CIDR_FILE="${CI_BURST_ROUTER_CIDR_FILE:-/etc/ci-burst-router/worker-cidrs}"
IFACE=$(ip route show default | awk '{print $5; exit}')
[ -n "${IFACE}" ] || { echo "no default interface" >&2; exit 1; }
sysctl -q -w net.ipv4.ip_forward=1
# -C and -A take the chain immediately, so the table selector comes first.
ensure_rule() {
  local table="$1"
  shift
  iptables -t "${table}" -C "$@" 2>/dev/null || iptables -t "${table}" -A "$@"
}
ensure_rule filter FORWARD -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
while read -r cidr; do
  [ -n "${cidr}" ] || continue
  ensure_rule nat POSTROUTING -s "${cidr}" -o "${IFACE}" -j MASQUERADE
  ensure_rule filter FORWARD -s "${cidr}" -o "${IFACE}" -j ACCEPT
  if ip link show wg0 >/dev/null 2>&1; then
    ensure_rule nat POSTROUTING -s "${cidr}" -o wg0 -j MASQUERADE
    ensure_rule filter FORWARD -s "${cidr}" -o wg0 -j ACCEPT
  fi
done < "${CIDR_FILE}"
iptables -t nat -S POSTROUTING
FORWARD
chmod 0755 "${FORWARD}"

cat > "${UNIT}" <<'UNIT'
[Unit]
Description=ci-burst router forwarding for the configured worker subnets
Documentation=https://github.com/mikomiel/ci-burst/blob/main/docs/router.md
After=network-online.target wg-quick@wg0.service
Wants=network-online.target

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/local/sbin/ci-burst-router-forward

[Install]
WantedBy=multi-user.target
UNIT
chmod 0644 "${UNIT}"
printf 'net.ipv4.ip_forward=1\n' > /etc/sysctl.d/90-ci-burst-router.conf

echo "STEP enable"
if [ -s "${WG_FILE}" ]; then
  systemctl enable --now wg-quick@wg0.service
fi
systemctl daemon-reload
systemctl enable --now ci-burst-router-forward.service
echo ROUTER_READY
