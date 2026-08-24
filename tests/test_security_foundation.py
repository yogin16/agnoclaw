from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from agnoclaw.runtime import (
    AdmissionEnvelope,
    AuthorizationGrant,
    DataClassification,
    GrantScope,
    HarnessError,
    IdentityAssertion,
    IdentitySource,
    KeyProvider,
    KeyPurpose,
    KeyReference,
    ModelAccess,
    PersistenceControl,
    SafeDiagnostic,
    SealedContent,
    TelemetryControl,
    data_handling_for,
    thaw_data,
)


def test_admission_envelope_resolves_one_deep_frozen_identity() -> None:
    claims = IdentityAssertion(
        source=IdentitySource.AUTHENTICATED_CLAIMS,
        tenant_id="tenant-1",
        user_id="user-1",
        session_id="session-1",
        roles=("developer",),
        scopes=("runs:write",),
    )
    request = IdentityAssertion(
        source=IdentitySource.REQUEST_PATH_BODY,
        tenant_id="tenant-1",
        user_id="user-1",
        session_id="session-1",
    )
    client_metadata = {"nested": {"items": ["one"]}}

    envelope = AdmissionEnvelope.resolve(
        claims,
        request,
        client_metadata=client_metadata,
        trusted_metadata={"permission": {"grant_ids": ["grant-1"]}},
        require_trusted_tenant=True,
        require_user=True,
    )

    assert envelope.digest == AdmissionEnvelope.from_dict(envelope.to_dict()).digest
    assert envelope.digest.startswith("sha256:")
    retried = AdmissionEnvelope.resolve(
        claims,
        request,
        request_id="new-request",
        trace_id="new-trace",
        client_metadata={"attempt": 2},
        trusted_metadata={"permission": {"grant_ids": ["grant-1"]}},
        require_trusted_tenant=True,
        require_user=True,
    )
    assert retried.digest != envelope.digest
    assert retried.authority_digest == envelope.authority_digest
    client_metadata["nested"]["items"].append("two")

    assert envelope.identity.tenant_id == "tenant-1"
    assert envelope.identity.roles == ("developer",)
    assert thaw_data(envelope.client_metadata) == {"nested": {"items": ["one"]}}
    with pytest.raises(TypeError):
        envelope.client_metadata["new"] = "value"
    with pytest.raises(TypeError):
        envelope.client_metadata["nested"]["items"] += ("two",)
    with pytest.raises(FrozenInstanceError):
        envelope.request_id = "replacement"


def test_conflicting_identity_values_fail_without_echoing_values() -> None:
    with pytest.raises(HarnessError) as caught:
        AdmissionEnvelope.resolve(
            IdentityAssertion(
                source=IdentitySource.AUTHENTICATED_CLAIMS,
                tenant_id="secret-tenant-a",
            ),
            IdentityAssertion(
                source=IdentitySource.REQUEST_PATH_BODY,
                tenant_id="secret-tenant-b",
            ),
        )

    error = caught.value
    assert error.code == "IDENTITY_CLAIM_CONFLICT"
    assert error.details == {"field": "tenant_id"}
    assert "secret-tenant" not in str(error)


def test_untrusted_roles_and_scopes_are_rejected() -> None:
    with pytest.raises(HarnessError) as caught:
        IdentityAssertion(
            source=IdentitySource.REQUEST_PATH_BODY,
            roles=("admin",),
        )
    assert caught.value.code == "IDENTITY_AUTHORITY_REQUIRED"


def test_service_scope_requires_authoritative_tenant() -> None:
    with pytest.raises(HarnessError) as caught:
        AdmissionEnvelope.resolve(
            IdentityAssertion(
                source=IdentitySource.CALLER_ARGUMENT,
                tenant_id="tenant-1",
                user_id="user-1",
            ),
            require_trusted_tenant=True,
        )
    assert caught.value.code == "TRUSTED_TENANT_REQUIRED"


def test_blank_identity_is_rejected_before_service_admission() -> None:
    with pytest.raises(HarnessError) as caught:
        IdentityAssertion(
            source=IdentitySource.TRUSTED_HOST,
            tenant_id="",
        )
    assert caught.value.code == "IDENTITY_VALUE_INVALID"


def test_authorization_grant_is_exactly_bound_and_immutable() -> None:
    grant = AuthorizationGrant(
        grant_id="grant-1",
        scope=GrantScope.RUN,
        tenant_id="tenant-1",
        principal_id="user-1",
        session_id="session-1",
        run_id="run-1",
        capability_ids=("shell", "shell"),
        capability_digests=("sha256:shell-v1",),
        effect_categories=("execute",),
        argument_digest="sha256:args",
        policy_version="policy-v1",
        authority_digest="sha256:authority",
        issuer="approver-1",
        expires_at="2026-08-07T12:00:00Z",
        nonce="nonce-1",
        issued_at="2026-08-07T11:00:00Z",
    )
    assert grant.capability_ids == ("shell",)
    assert AuthorizationGrant.from_dict(grant.to_dict()).digest == grant.digest
    with pytest.raises(FrozenInstanceError):
        grant.run_id = "run-2"


def test_run_scoped_authorization_grant_requires_run_and_capability() -> None:
    with pytest.raises(ValueError, match="run_id"):
        AuthorizationGrant(
            grant_id="grant-1",
            scope=GrantScope.RUN,
            tenant_id="tenant-1",
            principal_id="user-1",
            session_id="session-1",
            run_id=None,
            capability_ids=("shell",),
            capability_digests=("sha256:shell-v1",),
            effect_categories=(),
            argument_digest=None,
            policy_version="policy-v1",
            authority_digest="sha256:authority",
            issuer="approver-1",
            expires_at="2026-08-07T12:00:00Z",
            nonce="nonce-1",
            issued_at="2026-08-07T11:00:00Z",
        )


@pytest.mark.parametrize(
    ("classification", "model", "persistence", "telemetry"),
    [
        (
            DataClassification.PUBLIC,
            ModelAccess.ALLOW,
            PersistenceControl.PLAINTEXT_ALLOWED,
            TelemetryControl.CONTENT_ALLOWED,
        ),
        (
            DataClassification.INTERNAL,
            ModelAccess.ALLOW,
            PersistenceControl.PLAINTEXT_ALLOWED,
            TelemetryControl.METADATA_ONLY,
        ),
        (
            DataClassification.CONFIDENTIAL,
            ModelAccess.REQUIRE_POLICY,
            PersistenceControl.ENCRYPTION_REQUIRED,
            TelemetryControl.METADATA_ONLY,
        ),
        (
            DataClassification.RESTRICTED,
            ModelAccess.DENY,
            PersistenceControl.ENCRYPTION_REQUIRED,
            TelemetryControl.FORBIDDEN,
        ),
        (
            DataClassification.CREDENTIAL,
            ModelAccess.DENY,
            PersistenceControl.REFERENCE_ONLY,
            TelemetryControl.FORBIDDEN,
        ),
    ],
)
def test_data_classifications_have_non_ambiguous_default_handling(
    classification,
    model,
    persistence,
    telemetry,
) -> None:
    rule = data_handling_for(classification)
    assert rule.model_access == model
    assert rule.persistence == persistence
    assert rule.telemetry == telemetry


def test_safe_diagnostic_does_not_copy_raw_message_or_sensitive_details() -> None:
    error = HarnessError(
        code="PROVIDER_FAILED",
        category="provider",
        message="Authorization Bearer super-secret failed for /private/path",
        retryable=True,
        details={
            "provider": "example",
            "exception_type": "ProviderException",
            "token": "super-secret",
            "nested": {"prompt": "private"},
        },
    )

    diagnostic = SafeDiagnostic.from_error(
        error,
        safe_message="The provider request failed.",
        safe_details={
            "provider": "example",
            "exception_type": "ProviderException",
        },
        help_actions=("Retry after checking provider status.",),
        debug_reference="debug-123",
    )

    assert diagnostic.safe_message == "The provider request failed."
    assert dict(diagnostic.details) == {
        "provider": "example",
        "exception_type": "ProviderException",
    }
    assert "super-secret" not in repr(diagnostic)
    with pytest.raises(TypeError):
        diagnostic.details["provider"] = "changed"


def test_key_provider_contract_never_requires_raw_key_export() -> None:
    class FakeKeyProvider:
        def seal(self, plaintext, *, tenant_id, purpose, aad):
            del plaintext, aad
            return SealedContent(
                key=KeyReference("key-1", "v1", tenant_id, purpose),
                algorithm="fake-test-only",
                nonce_b64="bm9uY2U=",
                ciphertext_b64="Y2lwaGVydGV4dA==",
                aad_digest="sha256:test",
            )

        def unseal(self, content, *, aad):
            del content, aad
            return b"plaintext"

        def destroy(self, key):
            del key

    provider = FakeKeyProvider()
    assert isinstance(provider, KeyProvider)
    sealed = provider.seal(
        b"plaintext",
        tenant_id="tenant-1",
        purpose=KeyPurpose.RUNTIME_CONTENT,
        aad=b"scope",
    )
    assert sealed.key.tenant_id == "tenant-1"
    assert provider.unseal(sealed, aad=b"scope") == b"plaintext"


def test_admission_rejects_opaque_metadata_objects() -> None:
    with pytest.raises(TypeError, match="JSON-like"):
        AdmissionEnvelope.resolve(client_metadata={"live": object()})


def test_admission_rejects_non_string_metadata_keys() -> None:
    with pytest.raises(TypeError, match="keys must be strings"):
        AdmissionEnvelope.resolve(client_metadata={1: "ambiguous"})


def test_admission_rejects_non_finite_metadata_numbers() -> None:
    with pytest.raises(TypeError, match="finite"):
        AdmissionEnvelope.resolve(client_metadata={"value": float("nan")})


def test_admission_internal_projection_round_trips_provenance() -> None:
    original = AdmissionEnvelope.resolve(
        IdentityAssertion(
            source=IdentitySource.AUTHENTICATED_CLAIMS,
            tenant_id="tenant-1",
            user_id="user-1",
            roles=("developer",),
        ),
        request_id="request-1",
        client_metadata={"source": "sdk"},
    )

    restored = AdmissionEnvelope.from_dict(original.to_dict())

    assert restored == original
    assert restored.provenance[0].source == IdentitySource.AUTHENTICATED_CLAIMS
