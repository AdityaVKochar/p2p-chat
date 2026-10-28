#!/usr/bin/env bash
# Deploy a public CNP2P relay to a small EC2 instance using the AWS CLI.
#
# Creates (or reuses) a security group, an SSH key pair, and an Elastic IP,
# then launches Amazon Linux 2023 with a systemd service that runs
# `cnp2p --relay-server --daemon`. Remove everything with teardown-relay.sh.
#
# Settings (environment variables):
#   AWS_REGION      region to use (default: the AWS CLI's configured region)
#   RELAY_NAME      name tag and resource prefix (default: cnp2p-relay)
#   RELAY_PORT      public TCP port (default: 9100)
#   INSTANCE_TYPE   EC2 instance type (default: t4g.nano)
#   CNP2P_REF       git commit, branch, or tag to install (default: current HEAD;
#                   it must already be pushed to GitHub)
#   ADMIN_CIDR      address range allowed to SSH in (default: your public IP/32)
#   KEY_FILE        where the SSH private key is saved (default: ~/.ssh/$RELAY_NAME.pem)
set -euo pipefail

REGION=${AWS_REGION:-$(aws configure get region)}
NAME=${RELAY_NAME:-cnp2p-relay}
PORT=${RELAY_PORT:-9100}
INSTANCE_TYPE=${INSTANCE_TYPE:-t4g.nano}
REF=${CNP2P_REF:-$(git rev-parse HEAD)}
REPO=https://github.com/AdityaVKochar/p2p-chat
KEY_FILE=${KEY_FILE:-$HOME/.ssh/$NAME.pem}
ADMIN_CIDR=${ADMIN_CIDR:-$(curl -fsS https://checkip.amazonaws.com | tr -d '[:space:]')/32}
TAGS="{Key=Name,Value=$NAME},{Key=Project,Value=cnp2p}"

ec2() { aws ec2 --region "$REGION" "$@"; }
log() { printf '==> %s\n' "$*"; }

if ! curl -fsSIL -o /dev/null "$REPO/archive/$REF.tar.gz"; then
  echo "Commit $REF is not on GitHub yet; push it first or set CNP2P_REF." >&2
  exit 1
fi

existing=$(ec2 describe-instances \
  --filters "Name=tag:Name,Values=$NAME" "Name=instance-state-name,Values=pending,running,stopping,stopped" \
  --query 'Reservations[].Instances[].InstanceId' --output text)
if [[ -n "$existing" ]]; then
  echo "A relay instance named $NAME already exists ($existing). Run teardown-relay.sh first." >&2
  exit 1
fi

vpc=$(ec2 describe-vpcs --filters Name=is-default,Values=true --query 'Vpcs[0].VpcId' --output text)
[[ "$vpc" != "None" ]] || { echo "No default VPC in $REGION." >&2; exit 1; }

log "Security group"
sg=$(ec2 describe-security-groups --filters "Name=group-name,Values=$NAME" "Name=vpc-id,Values=$vpc" \
  --query 'SecurityGroups[0].GroupId' --output text)
if [[ "$sg" == "None" ]]; then
  sg=$(ec2 create-security-group --group-name "$NAME" --vpc-id "$vpc" \
    --description "CNP2P relay: public relay port, SSH from admin address" \
    --tag-specifications "ResourceType=security-group,Tags=[$TAGS]" --query GroupId --output text)
fi
authorize() {
  ec2 authorize-security-group-ingress --group-id "$sg" --ip-permissions "$1" >/dev/null 2>&1 || true
}
authorize "IpProtocol=tcp,FromPort=$PORT,ToPort=$PORT,IpRanges=[{CidrIp=0.0.0.0/0,Description=CNP2P relay}]"
authorize "IpProtocol=tcp,FromPort=22,ToPort=22,IpRanges=[{CidrIp=$ADMIN_CIDR,Description=SSH admin}]"

log "SSH key pair"
if ! ec2 describe-key-pairs --key-names "$NAME" >/dev/null 2>&1; then
  mkdir -p "$(dirname "$KEY_FILE")"
  umask 077
  ec2 create-key-pair --key-name "$NAME" --key-type ed25519 \
    --tag-specifications "ResourceType=key-pair,Tags=[$TAGS]" \
    --query KeyMaterial --output text > "$KEY_FILE"
  chmod 600 "$KEY_FILE"
elif [[ ! -f "$KEY_FILE" ]]; then
  echo "Key pair $NAME exists in AWS but $KEY_FILE is missing; set KEY_FILE or delete the key pair." >&2
  exit 1
fi

log "Elastic IP"
allocation=$(ec2 describe-addresses --filters "Name=tag:Name,Values=$NAME" \
  --query 'Addresses[0].AllocationId' --output text)
if [[ "$allocation" == "None" ]]; then
  allocation=$(ec2 allocate-address --domain vpc \
    --tag-specifications "ResourceType=elastic-ip,Tags=[$TAGS]" --query AllocationId --output text)
fi
public_ip=$(ec2 describe-addresses --allocation-ids "$allocation" --query 'Addresses[0].PublicIp' --output text)

arch=x86_64
[[ "$INSTANCE_TYPE" =~ ^[a-z][0-9]+g ]] && arch=arm64
ami=$(ec2 describe-images --owners amazon \
  --filters "Name=name,Values=al2023-ami-2023.*-kernel-6.*-$arch" "Name=state,Values=available" \
  --query 'sort_by(Images,&CreationDate)[-1].ImageId' --output text)

user_data=$(mktemp)
trap 'rm -f "$user_data"' EXIT
cat > "$user_data" <<EOF
#!/bin/bash
set -euxo pipefail
dnf install -y python3.11 python3.11-pip
useradd --system --home-dir /var/lib/cnp2p --create-home --shell /sbin/nologin cnp2p
python3.11 -m venv /opt/cnp2p
/opt/cnp2p/bin/pip install --upgrade pip
/opt/cnp2p/bin/pip install "$REPO/archive/$REF.tar.gz"
cat > /etc/systemd/system/cnp2p-relay.service <<'UNIT'
[Unit]
Description=CNP2P encrypted store-and-forward relay
After=network-online.target
Wants=network-online.target

[Service]
User=cnp2p
Group=cnp2p
Environment=PYTHONUNBUFFERED=1
ExecStart=/opt/cnp2p/bin/cnp2p --name "CNP2P Relay" --host 0.0.0.0 --port $PORT --advertise-host $public_ip --data-dir /var/lib/cnp2p --relay-server --no-lan --no-history --daemon
Restart=always
RestartSec=5
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true
ReadWritePaths=/var/lib/cnp2p

[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable --now cnp2p-relay
EOF

log "Launching $INSTANCE_TYPE ($ami) in $REGION"
instance=$(ec2 run-instances \
  --image-id "$ami" \
  --instance-type "$INSTANCE_TYPE" \
  --key-name "$NAME" \
  --security-group-ids "$sg" \
  --user-data "file://$user_data" \
  --credit-specification CpuCredits=standard \
  --metadata-options HttpTokens=required,HttpEndpoint=enabled \
  --block-device-mappings '[{"DeviceName":"/dev/xvda","Ebs":{"VolumeSize":8,"VolumeType":"gp3","Encrypted":true,"DeleteOnTermination":true}}]' \
  --tag-specifications "ResourceType=instance,Tags=[$TAGS]" "ResourceType=volume,Tags=[$TAGS]" \
  --query 'Instances[0].InstanceId' --output text)
ec2 wait instance-running --instance-ids "$instance"
ec2 associate-address --instance-id "$instance" --allocation-id "$allocation" >/dev/null

log "Waiting for the relay to accept connections on $public_ip:$PORT"
for _ in $(seq 1 60); do
  if timeout 3 bash -c "</dev/tcp/$public_ip/$PORT" 2>/dev/null; then
    cat <<EOF

Relay is up.
  Address:   $public_ip:$PORT
  Instance:  $instance ($INSTANCE_TYPE, $REGION)
  Installed: $REF
  SSH:       ssh -i $KEY_FILE ec2-user@$public_ip
  Logs:      ssh -i $KEY_FILE ec2-user@$public_ip sudo journalctl -u cnp2p-relay -f

In the desktop app, open Settings and set both "Bootstrap node" and "Relay" to $public_ip:$PORT.
EOF
    exit 0
  fi
  sleep 5
done
echo "The instance is running but the relay port did not open within 5 minutes." >&2
echo "Inspect the boot log with: aws ec2 get-console-output --region $REGION --instance-id $instance --latest" >&2
exit 1
