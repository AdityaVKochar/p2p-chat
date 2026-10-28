#!/usr/bin/env bash
# Remove everything deploy-relay.sh created: the instance, Elastic IP,
# security group, and key pair. Queued relay messages are deleted with the
# instance's disk.
set -euo pipefail

REGION=${AWS_REGION:-$(aws configure get region)}
NAME=${RELAY_NAME:-cnp2p-relay}
KEY_FILE=${KEY_FILE:-$HOME/.ssh/$NAME.pem}

ec2() { aws ec2 --region "$REGION" "$@"; }
log() { printf '==> %s\n' "$*"; }

instances=$(ec2 describe-instances \
  --filters "Name=tag:Name,Values=$NAME" "Name=instance-state-name,Values=pending,running,stopping,stopped" \
  --query 'Reservations[].Instances[].InstanceId' --output text)
if [[ -n "$instances" ]]; then
  log "Terminating $instances"
  # shellcheck disable=SC2086
  ec2 terminate-instances --instance-ids $instances >/dev/null
  # shellcheck disable=SC2086
  ec2 wait instance-terminated --instance-ids $instances
fi

for allocation in $(ec2 describe-addresses --filters "Name=tag:Name,Values=$NAME" \
  --query 'Addresses[].AllocationId' --output text); do
  log "Releasing Elastic IP $allocation"
  ec2 release-address --allocation-id "$allocation"
done

sg=$(ec2 describe-security-groups --filters "Name=group-name,Values=$NAME" \
  --query 'SecurityGroups[0].GroupId' --output text)
if [[ "$sg" != "None" ]]; then
  log "Deleting security group $sg"
  ec2 delete-security-group --group-id "$sg"
fi

if ec2 describe-key-pairs --key-names "$NAME" >/dev/null 2>&1; then
  log "Deleting key pair $NAME"
  ec2 delete-key-pair --key-name "$NAME"
  rm -f "$KEY_FILE"
fi

log "Relay $NAME removed from $REGION"
