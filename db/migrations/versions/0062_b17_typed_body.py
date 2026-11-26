"""Preserve B-17 typed receipt identity and inherited discount reason semantics."""

from alembic import op

revision = "0062_b17_typed_body"
down_revision = "0061_b17_cancel_replace"
branch_labels = None
depends_on = None


def _rewrite(signature: str, marker: str, replacement: str) -> None:
    connection = op.get_bind()
    definition = connection.exec_driver_sql(
        f"SELECT pg_get_functiondef('{signature}'::regprocedure)"
    ).scalar_one()
    if definition.count(marker) != 1:
        raise RuntimeError(f"Missing unique B-17 typed-body marker in {signature}")
    connection.exec_driver_sql(
        definition.replace(marker, replacement),
        execution_options={"no_parameters": True},
    )


def upgrade() -> None:
    # B-17 closes its intake authority before commit. The inherited deferred
    # D-05 checks must still see the immutable assignment and history evidence,
    # without reopening API read/write scope after finalization (as in 0049).
    op.execute("""
        ALTER FUNCTION public.assert_counter_assignment_complete() SECURITY DEFINER;
        ALTER FUNCTION public.assert_counter_assignment_complete()
            SET search_path TO pg_catalog, pg_temp;
        ALTER FUNCTION public.assert_assignment_history_complete() SECURITY DEFINER;
        ALTER FUNCTION public.assert_assignment_history_complete()
            SET search_path TO pg_catalog, pg_temp;
    """)
    op.execute("""
        GRANT CREATE ON SCHEMA public TO carepulse_identity_owner;
        SET LOCAL ROLE carepulse_identity_owner;
    """)
    _rewrite(
        "f01_private.b17_command(text,json)",
        "f01_private.b16_reason(d->>'discount_reason') IS NULL",
        r"""coalesce(btrim(d->>'discount_reason',
            U&'\0009\000A\000B\000C\000D\001C\001D\001E\001F\0020\0085\00A0\1680'
            || U&'\2000\2001\2002\2003\2004\2005\2006\2007\2008\2009\200A\2028\2029\202F\205F\3000'),'') = ''""",
    )
    _rewrite(
        "f01_private.b17_command(text,json)",
        "RETURN jsonb_strip_nulls(jsonb_set(command,'{reason}',to_jsonb(f01_private.b16_reason(command->>'reason'))));",
        "RETURN jsonb_set(command,'{reason}',to_jsonb(f01_private.b16_reason(command->>'reason')));",
    )
    # Keep explicit nulls in the signed digest, while optional null revision
    # tokens retain their previous meaning of absent for the execution guards.
    _rewrite(
        "public.f01_register_proof(bytea,text,bytea,bytea)",
        "SET b17_body=command,",
        "SET b17_body=jsonb_strip_nulls(command),",
    )
    op.execute("""
        RESET ROLE;
        REVOKE CREATE ON SCHEMA public FROM carepulse_identity_owner;
    """)


def downgrade() -> None:
    raise RuntimeError("B-17 typed body correction is forward-only")
