"""Consent Kernel v1: consent tables, append-only audit ledger, token
revocation list, downstream cascade contract, database roles.

Defence in depth for the audit ledger, outermost first:
  1. privileges  the runtime role remembrance_app holds only SELECT/INSERT
                 on audit_event; UPDATE/DELETE/TRUNCATE are revoked from it
                 and from PUBLIC.
  2. triggers    reject UPDATE/DELETE/TRUNCATE even for the table owner.
  3. hash chain  detects edits made by a superuser who bypasses 1 and 2
                 (scripts/verify_audit_chain.py).

Roles are cluster-wide and created only if missing; login users are granted
membership by operations (see README). Downgrade leaves roles in place.

Revision ID: 0001
Revises:
Create Date: 2026-09-28
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

APP_ROLE = "remembrance_app"
TOKEN_READER_ROLE = "remembrance_token_reader"

GRANTOR_TYPES = ("EXECUTOR", "ADMINISTRATOR", "BENEFICIARY", "SELF_PRE_NEED")
AUDIT_EVENT_TYPES = (
    "CONSENT_CREATED",
    "CONSENT_REVOKED",
    "AUTHORITY_VERIFIED",
    "SYNTHESIS_REQUESTED",
    "SYNTHESIS_BLOCKED",
    "MODEL_DELETED",
    "DATA_EXPORTED",
    "PURPOSE_VIOLATION_ATTEMPT",
    "PROFILE_CREATED",
    "DEATH_RECORDED",
    "BENEFICIARY_ACKNOWLEDGED",
    "SUCCESSOR_DESIGNATED",
    "AUTHORIZATION_GRANTED",
    "AUTHORIZATION_DENIED",
    "DELIVERY_CANCELLED",
    "REVOCATION_CASCADE_COMPLETED",
)
REVOCATION_STATUSES = ("PENDING", "PROCESSING", "COMPLETE")
CONSENT_ACTIONS = (
    "VIEW_MEMORIAL",
    "INITIATE_VOICE_SYNTHESIS",
    "GENERATE_SCRIPT",
    "DELIVER_MESSAGE",
    "EXPORT_DATA",
    "DELETE_VOICE_MODEL",
)
DELIVERY_STATUSES = ("SCHEDULED", "SENT", "CANCELLED", "FAILED")


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def _ts(name: str, nullable: bool = False) -> sa.Column:
    return sa.Column(name, sa.DateTime(timezone=True), nullable=nullable)


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        raise RuntimeError("Remembrance migrations target PostgreSQL only")

    op.create_table(
        "deceased_profile",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("full_name", sa.String(300), nullable=False),
        sa.Column("date_of_birth", sa.Date(), nullable=True),
        sa.Column("date_of_death", sa.Date(), nullable=True),
        sa.Column("jurisdiction_state", sa.String(10), nullable=False),
        _ts("created_at"),
        sa.PrimaryKeyConstraint("id", name="pk_deceased_profile"),
        sa.CheckConstraint(
            "date_of_birth IS NULL OR date_of_death IS NULL OR date_of_birth <= date_of_death",
            name="ck_deceased_profile_life_dates_ordered",
        ),
        sa.CheckConstraint("length(full_name) > 0", name="ck_deceased_profile_full_name_present"),
    )

    op.create_table(
        "consent_grant",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("deceased_profile_id", sa.Uuid(), nullable=False),
        sa.Column("grantor_type", sa.String(40), nullable=False),
        sa.Column("grantor_user_id", sa.String(255), nullable=False),
        sa.Column("grantor_legal_name", sa.String(300), nullable=False),
        sa.Column("grantor_contact_email", sa.String(320), nullable=False),
        sa.Column("legal_authority_document_url", sa.String(2048), nullable=False),
        _ts("authority_verified_at", nullable=True),
        sa.Column("authority_verified_by", sa.String(255), nullable=True),
        sa.Column("purpose_scope", postgresql.ARRAY(sa.String(32)), nullable=False),
        sa.Column("required_acknowledgments", sa.Integer(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        _ts("granted_at"),
        _ts("revoked_at", nullable=True),
        sa.Column("revocation_reason", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_consent_grant"),
        sa.ForeignKeyConstraint(
            ["deceased_profile_id"],
            ["deceased_profile.id"],
            name="fk_consent_grant_deceased_profile_id_deceased_profile",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(_in("grantor_type", GRANTOR_TYPES), name="ck_consent_grant_grantor_type"),
        sa.CheckConstraint("revoked_at IS NULL OR NOT is_active", name="ck_consent_grant_revoked_implies_inactive"),
        sa.CheckConstraint(
            "(authority_verified_at IS NULL) = (authority_verified_by IS NULL)",
            name="ck_consent_grant_verification_pair",
        ),
        sa.CheckConstraint(
            "revocation_reason IS NULL OR revoked_at IS NOT NULL",
            name="ck_consent_grant_reason_requires_revocation",
        ),
        sa.CheckConstraint(
            "required_acknowledgments IS NULL OR required_acknowledgments >= 0",
            name="ck_consent_grant_required_acks_nonnegative",
        ),
        sa.CheckConstraint(
            "cardinality(purpose_scope) > 0 AND purpose_scope <@ "
            "ARRAY['MEMORIAL_VIEW','VOICE_SYNTHESIS','SCRIPT_GENERATION']::varchar[]",
            name="ck_consent_grant_purpose_scope_valid",
        ),
    )
    op.create_index("ix_consent_grant_deceased_profile_id", "consent_grant", ["deceased_profile_id"])
    op.create_index("ix_consent_grant_grantor_user_id", "consent_grant", ["grantor_user_id"])

    op.create_table(
        "grant_beneficiary",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("consent_grant_id", sa.Uuid(), nullable=False),
        sa.Column("beneficiary_name", sa.String(300), nullable=False),
        sa.Column("beneficiary_email", sa.String(320), nullable=False),
        _ts("created_at"),
        sa.PrimaryKeyConstraint("id", name="pk_grant_beneficiary"),
        sa.ForeignKeyConstraint(
            ["consent_grant_id"],
            ["consent_grant.id"],
            name="fk_grant_beneficiary_consent_grant_id_consent_grant",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("consent_grant_id", "beneficiary_email", name="uq_grant_beneficiary_grant_email"),
    )

    op.create_table(
        "beneficiary_acknowledgment",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("consent_grant_id", sa.Uuid(), nullable=False),
        sa.Column("beneficiary_name", sa.String(300), nullable=False),
        sa.Column("beneficiary_email", sa.String(320), nullable=False),
        sa.Column("acknowledgment_signature_hash", sa.String(64), nullable=False),
        _ts("acknowledged_at"),
        sa.PrimaryKeyConstraint("id", name="pk_beneficiary_acknowledgment"),
        sa.ForeignKeyConstraint(
            ["consent_grant_id"],
            ["consent_grant.id"],
            name="fk_beneficiary_acknowledgment_consent_grant_id_consent_grant",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["consent_grant_id", "beneficiary_email"],
            ["grant_beneficiary.consent_grant_id", "grant_beneficiary.beneficiary_email"],
            name="fk_beneficiary_acknowledgment_named_beneficiary",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("consent_grant_id", "beneficiary_email", name="uq_beneficiary_ack_grant_email"),
        sa.CheckConstraint(
            "length(acknowledgment_signature_hash) = 64", name="ck_beneficiary_acknowledgment_signature_hash_sha256"
        ),
    )

    op.create_table(
        "audit_event",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("event_type", sa.String(40), nullable=False),
        sa.Column("actor_id", sa.String(255), nullable=False),
        sa.Column("consent_grant_id", sa.Uuid(), nullable=True),
        sa.Column("deceased_profile_id", sa.Uuid(), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("previous_hash", sa.String(64), nullable=False),
        sa.Column("event_hash", sa.String(64), nullable=False),
        _ts("created_at"),
        sa.PrimaryKeyConstraint("id", name="pk_audit_event"),
        sa.UniqueConstraint("event_hash", name="uq_audit_event_event_hash"),
        sa.UniqueConstraint("deceased_profile_id", "previous_hash", name="uq_audit_event_chain_link"),
        sa.CheckConstraint(_in("event_type", AUDIT_EVENT_TYPES), name="ck_audit_event_audit_event_type"),
        sa.CheckConstraint(
            "length(previous_hash) = 64 AND length(event_hash) = 64", name="ck_audit_event_hashes_sha256"
        ),
    )
    op.create_index("ix_audit_event_consent_grant_id", "audit_event", ["consent_grant_id"])
    op.create_index("ix_audit_event_profile_chain", "audit_event", ["deceased_profile_id", "id"])

    op.create_table(
        "revocation_request",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("consent_grant_id", sa.Uuid(), nullable=False),
        sa.Column("requested_by", sa.String(255), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("status", sa.String(40), nullable=False),
        _ts("created_at"),
        _ts("updated_at"),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        _ts("cascade_completed_at", nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_revocation_request"),
        sa.ForeignKeyConstraint(
            ["consent_grant_id"],
            ["consent_grant.id"],
            name="fk_revocation_request_consent_grant_id_consent_grant",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("consent_grant_id", name="uq_revocation_request_consent_grant_id"),
        sa.CheckConstraint(_in("status", REVOCATION_STATUSES), name="ck_revocation_request_revocation_status"),
        sa.CheckConstraint(
            "(status = 'COMPLETE') = (cascade_completed_at IS NOT NULL)",
            name="ck_revocation_request_completion_timestamp_matches_status",
        ),
    )
    op.create_index("ix_revocation_request_open", "revocation_request", ["status", "updated_at"])

    op.create_table(
        "authorization_token",
        sa.Column("jti", sa.Uuid(), nullable=False),
        sa.Column("consent_grant_id", sa.Uuid(), nullable=True),
        sa.Column("deceased_profile_id", sa.Uuid(), nullable=False),
        sa.Column("action", sa.String(40), nullable=False),
        sa.Column("purpose", sa.String(40), nullable=False),
        sa.Column("actor_id", sa.String(255), nullable=False),
        _ts("issued_at"),
        _ts("expires_at"),
        _ts("revoked_at", nullable=True),
        sa.PrimaryKeyConstraint("jti", name="pk_authorization_token"),
        sa.ForeignKeyConstraint(
            ["consent_grant_id"],
            ["consent_grant.id"],
            name="fk_authorization_token_consent_grant_id_consent_grant",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["deceased_profile_id"],
            ["deceased_profile.id"],
            name="fk_authorization_token_deceased_profile_id_deceased_profile",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(_in("action", CONSENT_ACTIONS), name="ck_authorization_token_consent_action"),
        sa.CheckConstraint("expires_at > issued_at", name="ck_authorization_token_expiry_after_issue"),
    )
    op.create_index("ix_authorization_token_consent_grant_id", "authorization_token", ["consent_grant_id"])

    op.create_table(
        "successor_designation",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("deceased_profile_id", sa.Uuid(), nullable=False),
        sa.Column("successor_user_id", sa.String(255), nullable=False),
        sa.Column("priority_order", sa.Integer(), nullable=False),
        _ts("designated_at"),
        sa.Column("designated_by", sa.String(255), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_successor_designation"),
        sa.ForeignKeyConstraint(
            ["deceased_profile_id"],
            ["deceased_profile.id"],
            name="fk_successor_designation_deceased_profile_id_deceased_profile",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("deceased_profile_id", "priority_order", name="uq_successor_profile_priority"),
        sa.UniqueConstraint("deceased_profile_id", "successor_user_id", name="uq_successor_profile_user"),
        sa.CheckConstraint("priority_order >= 1", name="ck_successor_designation_priority_positive"),
    )
    op.create_index(
        "ix_successor_designation_deceased_profile_id", "successor_designation", ["deceased_profile_id"]
    )

    op.create_table(
        "voice_model",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("consent_grant_id", sa.Uuid(), nullable=False),
        sa.Column("deceased_profile_id", sa.Uuid(), nullable=False),
        sa.Column("storage_key", sa.String(1024), nullable=True),
        _ts("created_at"),
        _ts("deleted_at", nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_voice_model"),
        sa.ForeignKeyConstraint(
            ["consent_grant_id"],
            ["consent_grant.id"],
            name="fk_voice_model_consent_grant_id_consent_grant",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["deceased_profile_id"],
            ["deceased_profile.id"],
            name="fk_voice_model_deceased_profile_id_deceased_profile",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "deleted_at IS NULL OR storage_key IS NULL", name="ck_voice_model_tombstone_has_no_key"
        ),
    )
    op.create_index("ix_voice_model_consent_grant_id", "voice_model", ["consent_grant_id"])

    op.create_table(
        "audio_artifact",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("consent_grant_id", sa.Uuid(), nullable=False),
        sa.Column("voice_model_id", sa.Uuid(), nullable=True),
        sa.Column("storage_key", sa.String(1024), nullable=True),
        _ts("created_at"),
        _ts("deleted_at", nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_audio_artifact"),
        sa.ForeignKeyConstraint(
            ["consent_grant_id"],
            ["consent_grant.id"],
            name="fk_audio_artifact_consent_grant_id_consent_grant",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["voice_model_id"],
            ["voice_model.id"],
            name="fk_audio_artifact_voice_model_id_voice_model",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "deleted_at IS NULL OR storage_key IS NULL", name="ck_audio_artifact_tombstone_has_no_key"
        ),
    )
    op.create_index("ix_audio_artifact_consent_grant_id", "audio_artifact", ["consent_grant_id"])

    op.create_table(
        "scheduled_delivery",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("consent_grant_id", sa.Uuid(), nullable=False),
        sa.Column("audio_artifact_id", sa.Uuid(), nullable=True),
        _ts("scheduled_for"),
        sa.Column("status", sa.String(40), nullable=False),
        _ts("cancelled_at", nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_scheduled_delivery"),
        sa.ForeignKeyConstraint(
            ["consent_grant_id"],
            ["consent_grant.id"],
            name="fk_scheduled_delivery_consent_grant_id_consent_grant",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["audio_artifact_id"],
            ["audio_artifact.id"],
            name="fk_scheduled_delivery_audio_artifact_id_audio_artifact",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(_in("status", DELIVERY_STATUSES), name="ck_scheduled_delivery_delivery_status"),
        sa.CheckConstraint(
            "(status = 'CANCELLED') = (cancelled_at IS NOT NULL)",
            name="ck_scheduled_delivery_cancellation_timestamp",
        ),
    )
    op.create_index("ix_scheduled_delivery_consent_grant_id", "scheduled_delivery", ["consent_grant_id"])

    _install_guards()
    _install_roles_and_grants()


def _install_guards() -> None:
    op.execute(
        """
        CREATE FUNCTION remembrance_reject_mutation() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION '% is append-only (% rejected)', TG_TABLE_NAME, TG_OP
                USING ERRCODE = 'insufficient_privilege';
        END
        $$;
        """
    )
    for table in ("audit_event", "beneficiary_acknowledgment"):
        op.execute(
            f"CREATE TRIGGER {table}_append_only BEFORE UPDATE OR DELETE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION remembrance_reject_mutation()"
        )
        op.execute(
            f"CREATE TRIGGER {table}_no_truncate BEFORE TRUNCATE ON {table} "
            "FOR EACH STATEMENT EXECUTE FUNCTION remembrance_reject_mutation()"
        )

    # Grant state machine: terms are immutable, verification is write-once,
    # revocation is terminal, rows are never deleted.
    op.execute(
        """
        CREATE FUNCTION consent_grant_guard() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'consent_grant rows are never deleted; revoke instead'
                    USING ERRCODE = 'insufficient_privilege';
            END IF;
            IF NEW.id IS DISTINCT FROM OLD.id
               OR NEW.deceased_profile_id IS DISTINCT FROM OLD.deceased_profile_id
               OR NEW.grantor_type IS DISTINCT FROM OLD.grantor_type
               OR NEW.grantor_user_id IS DISTINCT FROM OLD.grantor_user_id
               OR NEW.legal_authority_document_url IS DISTINCT FROM OLD.legal_authority_document_url
               OR NEW.purpose_scope IS DISTINCT FROM OLD.purpose_scope
               OR NEW.required_acknowledgments IS DISTINCT FROM OLD.required_acknowledgments
               OR NEW.granted_at IS DISTINCT FROM OLD.granted_at THEN
                RAISE EXCEPTION 'consent terms are immutable; create a new grant'
                    USING ERRCODE = 'check_violation';
            END IF;
            IF OLD.authority_verified_at IS NOT NULL
               AND (NEW.authority_verified_at IS DISTINCT FROM OLD.authority_verified_at
                    OR NEW.authority_verified_by IS DISTINCT FROM OLD.authority_verified_by) THEN
                RAISE EXCEPTION 'authority verification is write-once'
                    USING ERRCODE = 'check_violation';
            END IF;
            IF OLD.revoked_at IS NOT NULL
               AND (NEW.revoked_at IS DISTINCT FROM OLD.revoked_at
                    OR NEW.is_active
                    OR NEW.revocation_reason IS DISTINCT FROM OLD.revocation_reason) THEN
                RAISE EXCEPTION 'revocation is irreversible'
                    USING ERRCODE = 'check_violation';
            END IF;
            RETURN NEW;
        END
        $$;
        """
    )
    op.execute(
        "CREATE TRIGGER consent_grant_state_machine BEFORE UPDATE OR DELETE ON consent_grant "
        "FOR EACH ROW EXECUTE FUNCTION consent_grant_guard()"
    )

    # Tokens: only revoked_at may change, and only from NULL.
    op.execute(
        """
        CREATE FUNCTION authorization_token_guard() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'authorization_token rows are never deleted'
                    USING ERRCODE = 'insufficient_privilege';
            END IF;
            IF (NEW.jti, NEW.consent_grant_id, NEW.deceased_profile_id, NEW.action, NEW.purpose,
                NEW.actor_id, NEW.issued_at, NEW.expires_at)
               IS DISTINCT FROM
               (OLD.jti, OLD.consent_grant_id, OLD.deceased_profile_id, OLD.action, OLD.purpose,
                OLD.actor_id, OLD.issued_at, OLD.expires_at)
               OR (OLD.revoked_at IS NOT NULL AND NEW.revoked_at IS DISTINCT FROM OLD.revoked_at) THEN
                RAISE EXCEPTION 'issued tokens are immutable; revocation is write-once'
                    USING ERRCODE = 'check_violation';
            END IF;
            RETURN NEW;
        END
        $$;
        """
    )
    op.execute(
        "CREATE TRIGGER authorization_token_write_once BEFORE UPDATE OR DELETE ON authorization_token "
        "FOR EACH ROW EXECUTE FUNCTION authorization_token_guard()"
    )


def _install_roles_and_grants() -> None:
    for role in (APP_ROLE, TOKEN_READER_ROLE):
        op.execute(
            f"""
            DO $$ BEGIN
                IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{role}') THEN
                    CREATE ROLE {role} NOLOGIN;
                END IF;
            END $$;
            """
        )
    op.execute(
        f"DO $$ BEGIN EXECUTE format('GRANT USAGE ON SCHEMA %I TO {APP_ROLE}, {TOKEN_READER_ROLE}', "
        "current_schema()); END $$;"
    )
    tables = (
        "deceased_profile",
        "consent_grant",
        "grant_beneficiary",
        "beneficiary_acknowledgment",
        "audit_event",
        "revocation_request",
        "authorization_token",
        "successor_designation",
        "voice_model",
        "audio_artifact",
        "scheduled_delivery",
    )
    for table in tables:
        op.execute(f"REVOKE ALL ON {table} FROM PUBLIC")
    op.execute(
        f"GRANT SELECT, INSERT, UPDATE ON deceased_profile, consent_grant, revocation_request, "
        f"authorization_token, voice_model, audio_artifact, scheduled_delivery TO {APP_ROLE}"
    )
    op.execute(
        f"GRANT SELECT, INSERT ON grant_beneficiary, beneficiary_acknowledgment, successor_designation, "
        f"audit_event TO {APP_ROLE}"
    )
    op.execute(f"REVOKE UPDATE, DELETE, TRUNCATE ON audit_event FROM {APP_ROLE}")
    op.execute(f"GRANT USAGE, SELECT ON SEQUENCE audit_event_id_seq TO {APP_ROLE}")
    # Downstream services validate tokens against the revocation list and
    # nothing else: column-level read on authorization_token only.
    op.execute(
        "GRANT SELECT (jti, consent_grant_id, deceased_profile_id, action, actor_id, issued_at, "
        f"expires_at, revoked_at) ON authorization_token TO {TOKEN_READER_ROLE}"
    )


def downgrade() -> None:
    for table in (
        "scheduled_delivery",
        "audio_artifact",
        "voice_model",
        "successor_designation",
        "authorization_token",
        "revocation_request",
        "audit_event",
        "beneficiary_acknowledgment",
        "grant_beneficiary",
        "consent_grant",
        "deceased_profile",
    ):
        op.drop_table(table)
    op.execute("DROP FUNCTION IF EXISTS authorization_token_guard()")
    op.execute("DROP FUNCTION IF EXISTS consent_grant_guard()")
    op.execute("DROP FUNCTION IF EXISTS remembrance_reject_mutation()")
