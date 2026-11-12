"""T155: reject Unicode-blank reasons at protected Follow-up evidence writes."""

from alembic import op

revision = "0037_t155_followup_reason"
down_revision = "0036_b10_confirmation_parity"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # NOT VALID preserves existing immutable evidence but constrains every new row.
    whitespace = (
        r"U&'\0009\000A\000B\000C\000D\001C\001D\001E\001F\0020\0085\00A0\1680'"
        r" || U&'\2000\2001\2002\2003\2004\2005\2006\2007\2008\2009\200A\2028\2029\202F\205F\3000'"
    )
    for table in ("followup_source_decision_events", "followup_authorization_events"):
        op.execute(f"""
            ALTER TABLE public.{table}
            ADD CONSTRAINT {table}_t155_reason_nonblank
            CHECK (reason IS NULL OR btrim(reason, {whitespace}) <> '') NOT VALID
        """)


def downgrade() -> None:
    raise RuntimeError("T155 Follow-up evidence protection is forward-only")
