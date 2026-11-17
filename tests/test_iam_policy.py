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


def has_explicit_deny(policy, key, negated):
    """True when a Deny on ec2:RunInstances tests ``key`` the required way."""
    for statement in statements(policy):
        if statement.get("Effect") != "Deny" or "ec2:RunInstances" not in actions(statement):
            continue
        for operator, pairs in statement.get("Condition", {}).items():
            if key not in pairs:
                continue
            if key == "ec2:InstanceProfile":
                if operator == "Null" and str(pairs[key]).lower() == "false":
                    return True
            elif operator.endswith("NotEquals") == negated:
                return True
    return False


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

    def test_policy_omits_the_actions_the_design_leaves_out(self):
        every_action = {action.lower() for s in statements(self.policy) for action in actions(s)}
        self.assertFalse(every_action & FORBIDDEN_EC2)
        self.assertNotIn("ec2:modifyimageattribute", every_action)
        self.assertFalse([a for a in every_action if a.startswith("ssm:")])
        self.assertFalse([a for a in every_action if a == "*" or a.endswith(":*")])
        self.assertEqual(iam_authority_problems(self.policy), [])

    def test_instance_profile_is_denied_explicitly(self):
        self.assertTrue(has_explicit_deny(self.policy, "ec2:InstanceProfile", negated=False))

    def test_launch_outside_the_build_template_is_denied_explicitly(self):
        self.assertTrue(has_explicit_deny(self.policy, "ec2:LaunchTemplate", negated=True))
        for statement in statements(self.policy):
            if statement["Effect"] == "Deny":
                self.assertEqual(actions(statement), ["ec2:RunInstances"])

    def test_the_deny_checks_can_fail(self):
        for key, negated in (("ec2:InstanceProfile", False), ("ec2:LaunchTemplate", True)):
            with self.subTest(key=key):
                stripped = copy.deepcopy(self.policy)
                stripped["Statement"] = [
                    s for s in stripped["Statement"]
                    if not (s["Effect"] == "Deny"
                            and any(key in pairs for pairs in s.get("Condition", {}).values()))
                ]
                self.assertFalse(has_explicit_deny(stripped, key, negated))
        # The Deny must be a Deny, and the profile test must be Null = false.
        flipped = copy.deepcopy(self.policy)
        for statement in flipped["Statement"]:
            if "ec2:InstanceProfile" in statement.get("Condition", {}).get("Null", {}):
                statement["Condition"]["Null"]["ec2:InstanceProfile"] = "true"
        self.assertFalse(has_explicit_deny(flipped, "ec2:InstanceProfile", negated=False))

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
