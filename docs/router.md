# The optional router

A worker needs outbound HTTPS to the SSM endpoint and to the Forgejo instance.
Most VPC designs already provide that path — a NAT gateway, a NAT instance, a
firewall fleet, or a public subnet with a public address. This component is for
the design that has none of them: worker subnets route `0.0.0.0/0` to a single
small instance that rewrites the workers' source address.

You do not need it if a worker in your subnet can already reach
`https://ssm.<region>.amazonaws.com/` and your Forgejo endpoint. The cheapest
way to find out is the canary in step 6: launch one instance from the template
into the subnet and see whether it registers as a runner.

`scripts/check_prerequisites.py` checks that the configured route target still
carries the forwarding rules this document describes. It cannot prove
reachability; only a registration does.

## What it is, and what it is not

It is one EC2 instance that:

- is the default route target for the worker subnets;
- applies `MASQUERADE` and `FORWARD` rules for exactly the configured worker
  CIDRs, on the default interface and — when present — on `wg0`;
- optionally terminates a WireGuard tunnel to the network that hosts Forgejo;
- re-applies everything on boot from a systemd unit.

It is not part of the controller, it holds no launch permission, and it reads
exactly one SSM parameter. It never sees a job, a token or a repository.

```text
worker (private subnet)
   │  default route in the worker route table
   ▼
ENI of the router instance
   │  MASQUERADE for each worker CIDR
   ├── default interface ──► internet gateway ──► SSM
   └── wg0 (optional) ─────► the peer network ──► Forgejo
```

The failure this prevents is subtle: without `MASQUERADE`, a forwarded packet
keeps the worker's private source address. It leaves the router, and nothing
comes back, because neither the internet gateway nor the tunnel peer has a
return route for that address. The worker then reports
`Connect timeout on endpoint URL: "https://ssm.<region>.amazonaws.com/"` after
roughly three retry windows and shuts itself down.

## Artefacts

| File | Role |
| --- | --- |
| [`deploy/router/userdata.sh`](../deploy/router/userdata.sh) | first-boot script; the EC2 instance's user data |
| [`deploy/router/iam-policy.json`](../deploy/router/iam-policy.json) | the router's only permission: read one SSM parameter |

The script carries no deployment value and no secret, so the same bytes work in
every deployment. Everything specific is in the parameter.

## 1. Choose the worker CIDRs

These are the CIDRs of the subnets your deployment's `[[aws.availability_zones]]`
entries use. Every one of them must be forwarded; a subnet that is configured as
a candidate but missing from this list becomes a launch that never registers.

## 2. Write the configuration parameter

As an operator with `ssm:PutParameter`, create one SecureString. The optional
WireGuard block is the same text you would put in `/etc/wireguard/wg0.conf`:

```json
{
  "worker_cidrs": ["192.0.2.0/24", "198.51.100.0/24", "203.0.113.0/24"],
  "wireguard_config": "[Interface]\nPrivateKey = <key>\nAddress = <address>\n\n[Peer]\nPublicKey = <peer>\nAllowedIPs = <what the router may reach>\nEndpoint = <peer>:<port>\n"
}
```

```sh
aws ssm put-parameter --region <region> --name /ci-burst/router/config \
  --type SecureString --value file://router-config.json
```

The key lives in the parameter, never in user data. `ec2:DescribeInstanceAttribute`
returns user data to anyone holding that permission, and a WireGuard private key
in it is a leaked credential. Rotate the key when you move from a hand-built
router to this component.

## 3. Create the identity

Replace the account, region and parameter name placeholders in
[`deploy/router/iam-policy.json`](../deploy/router/iam-policy.json), attach it to
an IAM role, and give the instance an instance profile for that role. Nothing
else — no launch, no tag, no terminate.

## 4. Launch the router

The instance needs:

- **a public IPv4 address on the instance**, so the router itself can reach the
  package mirrors and SSM. A default route to an internet gateway is not enough:
  without an associated public address the gateway drops the router's own
  traffic. A subnet that auto-assigns public addresses,
  `--associate-public-ip-address`, or an Elastic IP all satisfy this, and the
  launch below asks for the address explicitly;
- a security group that allows **all** inbound from every worker CIDR and all
  outbound — the same shape as the NAT-instance security group in the AWS
  documentation;
- **source/destination checking disabled**, or forwarded packets are dropped;
- the instance profile from step 3;
- the user data from this repository.

```sh
aws ec2 run-instances --region <region> \
  --image-id <debian-image> --instance-type t4g.nano \
  --subnet-id <router-subnet> --security-group-ids <router-sg> \
  --associate-public-ip-address \
  --iam-instance-profile Name=<router-profile> \
  --user-data file://deploy/router/userdata.sh \
  --tag-specifications 'ResourceType=instance,Tags=[{Key=Name,Value=ci-burst-router}]'
aws ec2 modify-instance-attribute --region <region> \
  --no-source-dest-check --instance-id <instance-id>
```

`t4g.nano` is enough for a fleet of this size, but it is one instance: it is a
single point of failure for every worker's egress. If that matters, run one
router per availability zone and point each candidate's route at the router in
its own zone.

## 5. Point the route table at it

Add, or replace, the default route in the route table the worker subnets use:

```sh
aws ec2 replace-route --region <region> --route-table-id <worker-route-table> \
  --destination-cidr-block 0.0.0.0/0 --network-interface-id <router-eni>
```

Record the previous target first. The route target is the router's **network
interface**, not its instance id.

## 6. Verify with one canary per candidate

A clean `check_prerequisites` is not proof; a registration is. For each
configured candidate, launch one instance from the pinned template into that
candidate's subnet and watch for its runner:

```sh
aws ec2 run-instances --region <region> \
  --launch-template LaunchTemplateId=<id>,Version=<version> \
  --subnet-id <candidate-subnet> \
  --tag-specifications 'ResourceType=instance,Tags=[{Key=Name,Value=ci-burst-net-canary}]'
```

Within about 40 seconds the worker must appear in the repository's runner list
as `burst-ip-<private-ip-dashed>`. Its console output is the diagnostic when it
does not: `STEP mount-options` followed by `STEP register` is a working path.
`Connect timeout on endpoint URL` is a symptom of an unreachable path, not
proof of one cause — a missing forwarding rule for that CIDR, source/destination
checking still enabled, a security group or network ACL that does not admit the
subnet, or a router with no internet path of its own all look the same from the
worker.

Terminate the canary when the test is done. It self-terminates after its
lifetime, and the reaper does not manage it because it carries no
`ci-burst:managed` tag.

## 7. Prove it survives a reboot

The rules are a systemd unit, not a first-boot side effect. Reboot the instance
and confirm the tunnel and the rules return without logging in:

```sh
aws ec2 reboot-instances --region <region> --instance-ids <instance-id>
```

A reboot request is acknowledged, not completed, so wait for it to finish and
for the instance's status checks to pass again, then confirm from the console
output that the log begins at a new boot — if it still shows the previous boot,
the reboot has not happened and neither has the test. Then repeat the canary for
**every** configured candidate, not one: a router that cannot come back after a
reboot is a fleet that loses egress at the next host maintenance.

## 8. Replacing or removing the router

- **Replacing:** launch the replacement, disable its source/destination check,
  `replace-route` the default route to its interface, run the canaries, then
  stop and later terminate the old instance. Keep the old one stopped, not
  terminated, until both canaries pass.
- **Removing:** point the route at whatever provides the path instead, delete
  the parameter and the role, terminate the instance. Every worker keeps the
  configuration it started with, so nothing needs restarting.

## Cost and limits

A `t4g.nano` is a few dollars a month; the traffic it forwards is the workers'
own. The rules are per-CIDR source NAT, so the router is not a general egress
filter — it does not inspect, block or log traffic. If you need that, replace it
with a NAT gateway or a firewall appliance; this component is the smallest thing
that makes a private worker subnet work.
