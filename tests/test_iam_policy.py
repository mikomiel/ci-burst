"""Contract tests for the shipped IAM policies and the controller's IAM boundary.

The policies are configuration, so these tests read the committed JSON and the
package source. They need no credential and no network. The properties they
guard are the ones a later edit could silently remove:

* no shipped policy grants ``iam:*`` or any IAM action except one
  ``iam:PassRole`` statement for one role, conditioned on
  ``iam:PassedToService = ec2.amazonaws.com``;
* the image-build policy never grants the launch-template mutations and image
  sharing that the design leaves out, carries both explicit Denies on
  ``ec2:RunInstances``, scopes every mutating Allow by tag or by resource, and
  names the prefix placeholder in all S3 access;
* the controller package never constructs an IAM client;
* the documented source identity may assume the one image role and do nothing
  else.

Each checker also runs against a synthetic bad policy, so a green result means
the check can fail.
"""

import ast
import copy
import json
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
POLICY_DIR = ROOT / "deploy" / "iam"
IMAGE_POLICY = POLICY_DIR / "image-build-policy.json"
PACKAGE = ROOT / "ci_burst"
DEPLOYMENT_DOC = ROOT / "docs" / "deployment.md"

READ_VERBS = ("Describe", "Get", "List")
TAG_CONDITION = re.compile(
    r"^(?:ec2|aws):(?:ResourceTag|RequestTag)/|^ec2:CreateAction$"
)
PLACEHOLDERS = {
    "<account-id>",
    "<region>",
    "<deployment>",
    "<build-template-id>",
    "<worker-template-id>",
    "<build-subnet-id>",
    "<build-security-group-id>",
    "<base-image-id>",
    "<bucket>",
    "<prefix>",
}
BUILD_TEMPLATE_ARN = "arn:aws:ec2:<region>:<account-id>:launch-template/<build-template-id>"
INSTANCE_ARN = "arn:aws:ec2:<region>:<account-id>:instance/*"
PROFILE_DENY_CONDITION = {"Null": {"ec2:InstanceProfile": "false"}}
TEMPLATE_DENY_CONDITION = {"StringNotEquals": {"ec2:LaunchTemplate": BUILD_TEMPLATE_ARN}}
# Tag keys that images and snapshots must be allowed to carry. The spec names the
# first four keys; it names the last three only by meaning (source sha, script sha,
# base image), so the key names follow the ci-burst:<name> style of the others.
IMAGE_TAG_KEYS = {
    "ci-burst:image-build",
    "ci-burst:deployment",
    "ci-burst:build-id",
    "ci-burst:basis-digest",
    "ci-burst:source-sha",
    "ci-burst:script-sha",
    "ci-burst:base-image",
}
IMAGE_TAGGING_SIDS = ("TagOnlyOnCreation", "CreateOnlyTaggedImagesAndSnapshots")
OWNERSHIP_TAGS = {
    "ec2:ResourceTag/ci-burst:image-build": "true",
    "ec2:ResourceTag/ci-burst:deployment": "<deployment>",
}
OWNERSHIP_ACTIONS = (
    "ec2:TerminateInstances",
    "ec2:DeregisterImage",
    "ec2:DeleteSnapshot",
    "ec2:CreateImage",
)
FORBIDDEN_EC2 = {
    "ec2:modifylaunchtemplate",
    "ec2:deletelaunchtemplateversions",
}


def load(path):
    return json.loads(path.read_text(encoding="utf-8"))


def as_list(value):
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def statements(policy):
    return as_list(policy["Statement"])


def actions(statement):
    return [str(action) for action in as_list(statement.get("Action"))]


def resources(statement):
    return [str(resource) for resource in as_list(statement.get("Resource"))]


def shipped_policies():
    found = sorted(POLICY_DIR.glob("*.json"))
    if not found:
        raise AssertionError("no shipped policy under deploy/iam")
    return found


def iam_authority_problems(policy):
    """Name each IAM action that is not the one allowed PassRole form."""
    problems = []
    pass_role = []
    for statement in statements(policy):
        sid = statement.get("Sid", "<no sid>")
        for action in actions(statement):
            lowered = action.lower()
            if lowered == "*" or lowered.startswith("iam:") and "*" in lowered:
                problems.append(f"{sid}: wildcard {action}")
            elif lowered.startswith("iam:") and lowered != "iam:passrole":
                problems.append(f"{sid}: IAM action {action}")
            elif lowered == "iam:passrole":
                pass_role.append(statement)
            elif lowered in FORBIDDEN_EC2:
                problems.append(f"{sid}: {action}")
    for statement in pass_role:
        sid = statement.get("Sid", "<no sid>")
        role = resources(statement)
        condition = statement.get("Condition", {})
        if statement.get("Effect") != "Allow" or actions(statement) != ["iam:PassRole"]:
            problems.append(f"{sid}: iam:PassRole shares or denies a statement")
        if len(role) != 1 or "*" in role[0] or ":role/" not in role[0]:
            problems.append(f"{sid}: iam:PassRole must name one role ARN")
        if condition != {"StringEquals": {"iam:PassedToService": "ec2.amazonaws.com"}}:
            problems.append(f"{sid}: iam:PassRole needs the ec2.amazonaws.com condition")
    if len(pass_role) > 1:
        problems.append("more than one iam:PassRole statement")
    return problems


def has_explicit_deny(policy, condition):
    """True when a Deny on ec2:RunInstances matches ``condition`` exactly.

    The Deny must name only ``ec2:RunInstances`` (no NotAction or NotResource),
    cover the instance resource, and carry exactly ``condition``, so an extra
    narrowing condition or a moved resource scope does not count.
    """
    for statement in statements(policy):
        if statement.get("Effect") != "Deny":
            continue
        if "NotAction" in statement or "NotResource" in statement:
            continue
        if actions(statement) != ["ec2:RunInstances"]:
            continue
        if not {"*", INSTANCE_ARN} & set(resources(statement)):
            continue
        if statement.get("Condition") == condition:
            return True
    return False


def ownership_problems(policy):
    """Statements on destructive or image actions that lost a deployment tag guard."""
    problems = []
    for action in OWNERSHIP_ACTIONS:
        found = [
            s for s in statements(policy)
            if s.get("Effect") == "Allow" and action in actions(s)
            and any(key.startswith("ec2:ResourceTag/") for pairs in s.get("Condition", {}).values() for key in pairs)
        ]
        if not found:
            problems.append(f"{action}: no statement with a resource-tag guard")
        for statement in found:
            equals = statement["Condition"].get("StringEquals", {})
            for key, value in OWNERSHIP_TAGS.items():
                if equals.get(key) != value:
                    problems.append(f"{statement.get('Sid')}: {key} is not {value}")
    return problems


def image_tag_problems(policy):
    """Image-tagging statements that do not allow the full required key set."""
    problems = []
    by_sid = {s.get("Sid"): s for s in statements(policy)}
    for sid in IMAGE_TAGGING_SIDS:
        statement = by_sid.get(sid)
        if statement is None:
            problems.append(f"{sid}: missing")
            continue
        allowed = set(as_list(statement.get("Condition", {}).get("ForAllValues:StringEquals", {}).get("aws:TagKeys")))
        for key in sorted(IMAGE_TAG_KEYS - allowed):
            problems.append(f"{sid}: does not allow {key}")
    return problems


def mutating_allows(policy):
    for statement in statements(policy):
        if statement.get("Effect") != "Allow":
            continue
        if any(not action.split(":", 1)[1].startswith(READ_VERBS) for action in actions(statement)):
            yield statement


def unscoped_mutating(policy):
    """Mutating Allow statements with neither a tag condition nor a resource scope.

    A resource scope is a resource list with no wildcard, or an S3 object
    resource that names the ``<prefix>`` placeholder.
    """
    bad = []
    for statement in mutating_allows(policy):
        condition_keys = {
            key for pairs in statement.get("Condition", {}).values() for key in pairs
        }
        if any(TAG_CONDITION.match(key) for key in condition_keys):
            continue
        listed = resources(statement)
        if all("*" not in resource for resource in listed):
            continue
        if all("<prefix>" in resource for resource in listed):
            continue
        bad.append(statement.get("Sid", "<no sid>"))
    return bad


def s3_problems(policy):
    problems = []
    for statement in statements(policy):
        if not any(action.startswith("s3:") for action in actions(statement)):
            continue
        sid = statement.get("Sid", "<no sid>")
        if "*" in actions(statement) or any(a.endswith(":*") for a in actions(statement)):
            problems.append(f"{sid}: wildcard S3 action")
        for resource in resources(statement):
            if "<bucket>" not in resource:
                problems.append(f"{sid}: resource {resource} names no bucket placeholder")
            elif resource.count("/") and "<prefix>" not in resource:
                problems.append(f"{sid}: object resource {resource} names no prefix placeholder")
        if "s3:ListBucket" in actions(statement):
            prefixes = as_list(statement.get("Condition", {}).get("StringLike", {}).get("s3:prefix"))
            if not prefixes or any("<prefix>" not in value for value in prefixes):
                problems.append(f"{sid}: list is not restricted to the prefix placeholder")
    return problems


def security_group_read_problems(policy):
    """Why ``ec2:DescribeSecurityGroups`` is not one read-only Allow, or ``[]``.

    The build precheck reads the security groups its launch template names, so
    the grant must exist exactly once and, in that statement, no action may
    change anything. A describe action takes no resource scope, so the grant
    shares the describe-only statement.
    """
    action = "ec2:DescribeSecurityGroups"
    holders = [s for s in statements(policy) if action in actions(s)]
    if len(holders) != 1:
        return [f"{len(holders)} statements name {action}"]
    (holder,) = holders
    sid = holder.get("Sid", "<no sid>")
    problems = []
    if holder.get("Effect") != "Allow":
        problems.append(f"{sid}: {action} is not an Allow")
    for name in actions(holder):
        if not name.split(":", 1)[1].startswith(READ_VERBS):
            problems.append(f"{sid}: {name} is not a read")
    return problems


def iam_client_calls(source):
    """Line numbers where source builds an IAM client or resource."""
    found = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        name = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
        if name not in {"client", "resource", "create_client"}:
            continue
        values = list(node.args[:1]) + [
            keyword.value for keyword in node.keywords if keyword.arg == "service_name"
        ]
        for value in values:
            if isinstance(value, ast.Constant) and str(value.value).lower() == "iam":
                found.append(node.lineno)
    return found


class SharedPolicyTests(unittest.TestCase):
    def test_no_shipped_policy_has_iam_authority_beyond_one_scoped_pass_role(self):
        for path in shipped_policies():
            with self.subTest(policy=path.name):
                self.assertEqual(iam_authority_problems(load(path)), [])

    def test_checker_rejects_iam_authority(self):
        base = {"Statement": []}
        bad_actions = ["iam:*", "iam:CreateRole", "iam:Pass*", "*", "ec2:ModifyLaunchTemplate",
                       "ec2:DeleteLaunchTemplateVersions"]
        for action in bad_actions:
            with self.subTest(action=action):
                policy = copy.deepcopy(base)
                policy["Statement"].append({"Effect": "Allow", "Action": action, "Resource": "*"})
                self.assertTrue(iam_authority_problems(policy))

    def test_pass_role_is_accepted_only_in_its_one_scoped_form(self):
        role = "arn:aws:iam::<account-id>:role/<worker-role>"
        good = {
            "Sid": "PassOnlyTheWorkerRole",
            "Effect": "Allow",
            "Action": "iam:PassRole",
            "Resource": role,
            "Condition": {"StringEquals": {"iam:PassedToService": "ec2.amazonaws.com"}},
        }
        self.assertEqual(iam_authority_problems({"Statement": [good]}), [])
        variants = {
            "no condition": {k: v for k, v in good.items() if k != "Condition"},
            "wrong service": {**good, "Condition": {"StringEquals": {"iam:PassedToService": "ecs.amazonaws.com"}}},
            "wildcard role": {**good, "Resource": "arn:aws:iam::<account-id>:role/*"},
            "star resource": {**good, "Resource": "*"},
            "two roles": {**good, "Resource": [role, role + "-b"]},
            "shared statement": {**good, "Action": ["iam:PassRole", "ec2:DescribeInstances"]},
        }
        for label, statement in variants.items():
            with self.subTest(variant=label):
                self.assertTrue(iam_authority_problems({"Statement": [statement]}))
        with self.subTest(variant="two statements"):
            self.assertTrue(iam_authority_problems({"Statement": [good, copy.deepcopy(good)]}))


class ImageBuildPolicyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = IMAGE_POLICY.read_text(encoding="utf-8")
        cls.policy = json.loads(cls.text)

    def test_policy_uses_placeholders_only(self):
        self.assertNotRegex(self.text, r"\d{12}")
        self.assertNotRegex(self.text, r"\b(?:ami|lt|subnet|sg|vol|snap|i)-[0-9a-f]{8,}")
        self.assertEqual(set(re.findall(r"<[^<>]+>", self.text)) - PLACEHOLDERS, set())
        for placeholder in PLACEHOLDERS - {"<region>"}:
            self.assertIn(placeholder, self.text)
        self.assertIn("<region>", self.text)

    def test_every_statement_has_a_unique_sid_and_an_explicit_effect(self):
        sids = [statement.get("Sid") for statement in statements(self.policy)]
        self.assertNotIn(None, sids)
        self.assertEqual(len(sids), len(set(sids)))
        for statement in statements(self.policy):
            self.assertIn(statement.get("Effect"), {"Allow", "Deny"})

    def test_the_template_security_groups_can_be_read_by_a_read_only_statement(self):
        self.assertEqual(security_group_read_problems(self.policy), [])

    def test_the_security_group_read_check_can_fail(self):
        action = "ec2:DescribeSecurityGroups"

        def holder(policy):
            (found,) = [s for s in policy["Statement"] if action in actions(s)]
            return found

        absent = copy.deepcopy(self.policy)
        holder(absent)["Action"] = [name for name in actions(holder(absent)) if name != action]
        mutating = copy.deepcopy(self.policy)
        holder(mutating)["Action"] = [*actions(holder(mutating)), "ec2:ModifySecurityGroupRules"]
        denied = copy.deepcopy(self.policy)
        holder(denied)["Effect"] = "Deny"
        twice = copy.deepcopy(self.policy)
        twice["Statement"].append({"Sid": "Again", **{k: v for k, v in holder(twice).items() if k != "Sid"}})
        for label, policy in {"absent": absent, "mutating": mutating, "denied": denied, "twice": twice}.items():
            with self.subTest(variant=label):
                self.assertTrue(security_group_read_problems(policy))

    def test_policy_omits_the_actions_the_design_leaves_out(self):
        every_action = {action.lower() for s in statements(self.policy) for action in actions(s)}
        self.assertFalse(every_action & FORBIDDEN_EC2)
        self.assertNotIn("ec2:modifyimageattribute", every_action)
        self.assertFalse([a for a in every_action if a.startswith("ssm:")])
        self.assertFalse([a for a in every_action if a == "*" or a.endswith(":*")])
        self.assertEqual(iam_authority_problems(self.policy), [])

    def test_the_image_policy_has_no_iam_action_until_gate_g5(self):
        # Gate G5 may add one scoped iam:PassRole statement. That change must also
        # replace this assertion; the shared rule above already limits its form.
        every_action = [a for s in statements(self.policy) for a in actions(s)]
        self.assertEqual([a for a in every_action if a.lower().startswith("iam:")], [])

    def test_instance_profile_is_denied_explicitly(self):
        self.assertTrue(has_explicit_deny(self.policy, PROFILE_DENY_CONDITION))

    def test_launch_outside_the_build_template_is_denied_explicitly(self):
        self.assertTrue(has_explicit_deny(self.policy, TEMPLATE_DENY_CONDITION))
        for statement in statements(self.policy):
            if statement["Effect"] == "Deny":
                self.assertEqual(actions(statement), ["ec2:RunInstances"])

    def test_the_allow_and_the_template_deny_name_the_same_template(self):
        allowed = [
            s["Condition"]["ArnEquals"]["ec2:LaunchTemplate"]
            for s in statements(self.policy)
            if s["Effect"] == "Allow" and "ec2:RunInstances" in actions(s)
            and "ArnEquals" in s.get("Condition", {})
        ]
        self.assertEqual(allowed, [BUILD_TEMPLATE_ARN])

    def _deny_mutants(self, condition):
        """Policies in which the Deny for ``condition`` is broken in one way."""
        key = next(iter(next(iter(condition.values()))))

        def variant(change):
            policy = copy.deepcopy(self.policy)
            hit = [
                s for s in policy["Statement"]
                if s["Effect"] == "Deny"
                and any(key in pairs for pairs in s.get("Condition", {}).values())
            ]
            self.assertEqual(len(hit), 1)
            change(hit[0])
            return policy

        def drop(statement):
            statement["Effect"] = "Allow"

        def wrong_resource(statement):
            statement["Resource"] = "arn:aws:ec2:<region>:<account-id>:subnet/<build-subnet-id>"

        def not_resource(statement):
            del statement["Resource"]
            statement["NotResource"] = INSTANCE_ARN

        def other_action(statement):
            statement["Action"] = "ec2:StartInstances"

        def extra_narrowing(statement):
            statement["Condition"]["StringEquals"] = {"aws:RequestTag/ci-burst:image-build": "true"}

        def extra_key(statement):
            operator = next(iter(statement["Condition"]))
            statement["Condition"][operator]["ec2:Region"] = "<region>"

        def flip_operator(statement):
            operator = next(iter(statement["Condition"]))
            pairs = statement["Condition"].pop(operator)
            statement["Condition"]["Null" if operator != "Null" else "StringEquals"] = pairs

        def wrong_value(statement):
            operator = next(iter(statement["Condition"]))
            statement["Condition"][operator][key] = (
                "true" if operator == "Null" else BUILD_TEMPLATE_ARN.replace("<build-template-id>", "<worker-template-id>")
            )

        mutants = {
            "allow instead of deny": drop,
            "wrong resource scope": wrong_resource,
            "NotResource": not_resource,
            "wrong action": other_action,
            "extra narrowing condition": extra_narrowing,
            "extra key in the same operator": extra_key,
            "wrong operator": flip_operator,
            "wrong value or template": wrong_value,
        }
        return {label: variant(change) for label, change in mutants.items()}

    def test_the_deny_checks_can_fail(self):
        for condition in (PROFILE_DENY_CONDITION, TEMPLATE_DENY_CONDITION):
            for label, mutant in self._deny_mutants(condition).items():
                with self.subTest(condition=next(iter(condition.values())), mutant=label):
                    self.assertFalse(has_explicit_deny(mutant, condition))
        stripped = copy.deepcopy(self.policy)
        stripped["Statement"] = [s for s in stripped["Statement"] if s["Effect"] != "Deny"]
        self.assertFalse(has_explicit_deny(stripped, PROFILE_DENY_CONDITION))
        self.assertFalse(has_explicit_deny(stripped, TEMPLATE_DENY_CONDITION))

    def test_destructive_and_image_actions_keep_the_ownership_tags(self):
        self.assertEqual(ownership_problems(self.policy), [])

    def test_the_ownership_check_can_fail(self):
        for action in OWNERSHIP_ACTIONS:
            for dropped in OWNERSHIP_TAGS:
                with self.subTest(action=action, dropped=dropped):
                    policy = copy.deepcopy(self.policy)
                    changed = 0
                    for statement in policy["Statement"]:
                        pairs = statement.get("Condition", {}).get("StringEquals", {})
                        if action in actions(statement) and dropped in pairs:
                            del pairs[dropped]
                            changed += 1
                    self.assertEqual(changed, 1)
                    self.assertTrue(ownership_problems(policy))
        stripped = copy.deepcopy(self.policy)
        stripped["Statement"] = [s for s in stripped["Statement"] if "ec2:DeleteSnapshot" not in actions(s)]
        self.assertTrue(ownership_problems(stripped))

    def test_images_and_snapshots_may_carry_the_complete_tag_set(self):
        self.assertEqual(image_tag_problems(self.policy), [])

    def test_the_image_tag_check_can_fail(self):
        for sid in IMAGE_TAGGING_SIDS:
            for key in sorted(IMAGE_TAG_KEYS):
                with self.subTest(statement=sid, dropped=key):
                    policy = copy.deepcopy(self.policy)
                    for statement in policy["Statement"]:
                        if statement["Sid"] == sid:
                            statement["Condition"]["ForAllValues:StringEquals"]["aws:TagKeys"].remove(key)
                    self.assertEqual(
                        image_tag_problems(policy), [f"{sid}: does not allow {key}"]
                    )

    def test_every_mutating_allow_has_a_tag_or_resource_scope(self):
        self.assertEqual(unscoped_mutating(self.policy), [])
        self.assertTrue(list(mutating_allows(self.policy)))

    def test_the_scope_check_can_fail(self):
        for action, resource in (
            ("ec2:TerminateInstances", "arn:aws:ec2:<region>:<account-id>:instance/*"),
            ("ec2:RunInstances", "*"),
            ("s3:PutObject", "arn:aws:s3:::<bucket>/*"),
        ):
            with self.subTest(action=action):
                policy = {"Statement": [{"Effect": "Allow", "Action": action, "Resource": resource}]}
                self.assertEqual(unscoped_mutating(policy), [policy["Statement"][0].get("Sid", "<no sid>")])

    def test_s3_access_names_the_bucket_and_prefix_placeholders(self):
        self.assertEqual(s3_problems(self.policy), [])
        s3_statements = [s for s in statements(self.policy) if any(a.startswith("s3:") for a in actions(s))]
        self.assertTrue(s3_statements)
        object_resources = [r for s in s3_statements for r in resources(s) if "/" in r.split(":::", 1)[1]]
        self.assertTrue(object_resources)
        for resource in object_resources:
            self.assertTrue(resource.startswith("arn:aws:s3:::<bucket>/<prefix>"), resource)

    def test_the_s3_check_can_fail(self):
        for statement in (
            {"Effect": "Allow", "Action": "s3:PutObject", "Resource": "arn:aws:s3:::<bucket>/*"},
            {"Effect": "Allow", "Action": "s3:ListBucket", "Resource": "arn:aws:s3:::<bucket>"},
            {"Effect": "Allow", "Action": "s3:*", "Resource": "arn:aws:s3:::<bucket>/<prefix>*"},
            {"Effect": "Allow", "Action": "s3:GetObject", "Resource": "arn:aws:s3:::other/<prefix>*"},
        ):
            with self.subTest(statement=statement):
                self.assertTrue(s3_problems({"Statement": [statement]}))


class ControllerHasNoIamClientTests(unittest.TestCase):
    def test_package_constructs_no_iam_client(self):
        sources = sorted(PACKAGE.rglob("*.py"))
        self.assertTrue(sources, "the ci_burst package has no source to scan")
        for path in sources:
            with self.subTest(file=str(path.relative_to(ROOT))):
                self.assertEqual(iam_client_calls(path.read_text(encoding="utf-8")), [])

    def test_the_scan_finds_each_construction_form(self):
        forms = (
            'session.client("iam")',
            "session.client('iam', region_name=r)",
            'session.client(service_name="iam")',
            'boto3.client("IAM")',
            'boto3.resource("iam")',
            'client("iam")',
        )
        for form in forms:
            with self.subTest(form=form):
                self.assertTrue(iam_client_calls(f"x = {form}\n"))
        for fine in ('session.client("ec2")', 'session.client("sts")', '# session.client("iam")'):
            with self.subTest(fine=fine):
                self.assertEqual(iam_client_calls(f"x = 1\n{fine}\n"), [])


class DocumentedImageIdentityTests(unittest.TestCase):
    """The source-identity and trust-policy examples in docs/deployment.md."""

    @classmethod
    def setUpClass(cls):
        text = DEPLOYMENT_DOC.read_text(encoding="utf-8")
        match = re.search(r"^### Image-build role and source identity.*?(?=^#{1,3} )", text, re.S | re.M)
        assert match, "docs/deployment.md has no image-build role section"
        cls.section = match.group(0)
        blocks = re.findall(r"```json\n(.*?)```", cls.section, re.S)
        cls.documents = [json.loads(block) for block in blocks]

    def test_source_identity_may_only_assume_the_one_image_role(self):
        sources = [
            doc for doc in self.documents
            if any("sts:AssumeRole" in actions(s) and "Principal" not in s for s in statements(doc))
        ]
        self.assertEqual(len(sources), 1)
        (statement,) = statements(sources[0])
        self.assertEqual(statement["Effect"], "Allow")
        self.assertEqual(actions(statement), ["sts:AssumeRole"])
        (role,) = resources(statement)
        self.assertRegex(role, r"^arn:aws:iam::<account-id>:role/<[a-z-]+>$")
        self.assertEqual(iam_authority_problems(sources[0]), [])

    def test_trust_policy_names_one_principal_and_the_external_id(self):
        trusts = [
            doc for doc in self.documents
            if any("Principal" in s for s in statements(doc))
        ]
        self.assertEqual(len(trusts), 1)
        (statement,) = statements(trusts[0])
        self.assertEqual(statement["Effect"], "Allow")
        self.assertEqual(actions(statement), ["sts:AssumeRole"])
        principals = as_list(statement["Principal"]["AWS"])
        self.assertEqual(len(principals), 1)
        self.assertNotIn("*", principals[0])
        self.assertEqual(
            statement["Condition"], {"StringEquals": {"sts:ExternalId": "<external-id>"}}
        )

    def test_session_limit_is_documented(self):
        self.assertIn("3600", self.section)
        self.assertNotRegex(self.section, r"\d{12}")


if __name__ == "__main__":
    unittest.main()
