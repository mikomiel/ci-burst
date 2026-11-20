"""The inert launch-template version of a built image, and the check that it only changed the image.

After a build's image is ``available``, ``create_candidate_version`` creates one
new version of the worker launch template from the numeric pin the deployment
uses today. The request names only the new ``ImageId``, so the provider copies
every other field from that pin. IAM cannot limit what a version holds, so the
control is the comparison afterwards: both versions are read back and their
whole ``LaunchTemplateData`` is compared. Any difference but ``ImageId`` is
``TEMPLATE_DRIFT``.

The version is inert. It is never made the default and nothing is modified or
deleted: this module calls ``create_template_version`` and ``template_versions``
of the image adapter and nothing else, and the adapter has no call that sets a
default. A person promotes a version by editing the pin.

The module logs nothing and holds no state. A drift result names the top-level
fields that differ and never a value, because a value can be user data.
"""

from .errors import ProviderRefusal

CLEAN = "clean"
TEMPLATE_DRIFT = "TEMPLATE_DRIFT"

_IMAGE_FIELD = "ImageId"
_MISSING = object()


def create_candidate_version(
    aws, *, template_name, source_pin, image_id, build_id, basis_digest, client_token, deadline=None
):
    """Create the candidate's launch-template version and compare it with the pin.

    ``aws`` is an ``ImageAws``. ``source_pin`` is the numeric version the
    deployment's configuration pins now (never ``$Latest`` or the default; the
    adapter refuses anything but a positive integer, before any request).
    ``image_id`` is the new image. ``build_id`` and the first 12 characters of
    ``basis_digest`` go into the version description, and ``client_token`` is
    the build's token from the state file.

    The result is ``{"version": n, "drift": "clean"}``, or
    ``{"version": n, "drift": "TEMPLATE_DRIFT", "fields": [...]}`` with the sorted
    names of the top-level ``LaunchTemplateData`` keys that differ between the
    pin and the new version (``ImageId`` among them only when the new version
    does not carry ``image_id``). A key that exists in one version only is a
    difference. Names only, never values.

    A creation that answers with ``source_pin`` itself created nothing the
    comparison could use, so it is refused before any read-back, with
    ``AWS_UNCERTAIN`` and no verdict. Read back, the pin would be compared with
    itself and the image the pin already names would decide the result, so a pin
    that names the built image would pass as ``clean``.

    A read-back that does not return exactly the pin and the new version is
    refused with the provider's ``AWS_INVENTORY_INCOMPLETE`` and no verdict. The
    version may then exist already, so the caller retries on the next pass with
    the same ``client_token``: the provider returns the same version number for
    the same token, so a crash between creating and recording the version never
    creates a second one. A refusal of the provider passes unchanged.

    ``deadline`` bounds the read-back, which is a read like any other of the pass: one that would start at or
    after it raises ``DeadlineRefusal`` before any request. The creation is one call and cannot be bounded, so a
    pass that ran out of time during it ends here with the version in place, and the retry with the same
    ``client_token`` finds that version instead of creating another.
    """
    description = f"ci-burst {build_id} {basis_digest[:12]}"
    version = aws.create_template_version(template_name, source_pin, image_id, description, client_token)
    if version == source_pin:
        raise ProviderRefusal(
            "AWS_UNCERTAIN", f"version creation returned the source version {source_pin}", uncertain=True
        )
    versions = [source_pin, version]
    read = aws.template_versions(template_name, versions, deadline=deadline)
    by_number = {item.version: item for item in read}
    if len(by_number) != len(read) or set(by_number) != set(versions):
        raise ProviderRefusal(
            "AWS_INVENTORY_INCOMPLETE",
            f"the read-back did not return exactly versions {source_pin} and {version}",
            uncertain=True,
        )
    source, created = by_number[source_pin].data, by_number[version].data
    fields = {key for key in set(source) | set(created) if source.get(key, _MISSING) != created.get(key, _MISSING)}
    # The image is expected to differ, so only a wrong image counts: the new
    # version must carry exactly the image that was built.
    fields.discard(_IMAGE_FIELD)
    if created.get(_IMAGE_FIELD) != image_id:
        fields.add(_IMAGE_FIELD)
    if fields:
        return {"version": version, "drift": TEMPLATE_DRIFT, "fields": sorted(fields)}
    return {"version": version, "drift": CLEAN}
