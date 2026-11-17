# Spec Delta

## MODIFIED Requirements

### Requirement: Credentials remain in their intended boundary
Credential values MUST come from the operator's configured environment/file or the AWS SDK credential chain. Provisioning credentials MUST NOT reach a worker. Bootstrap credentials MUST remain the operator-owned image/template responsibility, not controller-generated user data or command arguments. The one exception is the image-build path: the controller MAY place one expiring presigned read URL for one staged source archive in the user data of a builder instance, and MUST NOT place any other credential, token or secret there. The image-build path MUST use its own assumed-role identity and its own Forgejo token, and MUST NOT reuse the scaling identity or the token of `apply`.

#### Scenario: Private deployment uses machine authentication
- **WHEN** an unattended controller is prepared for a live pilot
- **THEN** it uses an approved machine-identity route
- **AND** copying a human login cache is not a supported deployment step

#### Scenario: Logs or errors contain sensitive provider data
- **WHEN** a response includes an authorization value, token, secret configuration or user data
- **THEN** status and logs omit or redact that value
- **AND** raw response/configuration/environment dumps are not emitted

#### Scenario: A builder receives its source
- **WHEN** an image build launches a builder
- **THEN** its user data holds one presigned URL that expires within the configured window and no other credential
- **AND** the builder has no instance profile

#### Scenario: A worker is launched
- **WHEN** the controller launches a worker
- **THEN** no image-build credential, token or presigned URL reaches it
