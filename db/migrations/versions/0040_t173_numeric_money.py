"""Keep exact percentage snapshot guards within PostgreSQL NUMERIC's native range."""

from alembic import op

revision = "0040_t173_numeric_money"
down_revision = "0039_t160_capacity_numeric"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # CREATE OR REPLACE preserves the existing function owners and EXECUTE grants.
    op.execute(r"""
        CREATE OR REPLACE FUNCTION public.t145_decimal_compare_rational(
            value text, numerator numeric, denominator numeric)
        RETURNS integer
        LANGUAGE plpgsql IMMUTABLE PARALLEL SAFE STRICT
        SET search_path = pg_catalog AS $$
        DECLARE
            integer_part integer;
            fractional_part text;
            rational_integer numeric;
            remainder numeric;
            scale numeric;
            rational_chunk numeric;
            supplied_chunk numeric;
            digit_offset integer := 1;
            chunk_length integer;
            chunk_size integer;
        BEGIN
            IF denominator <= 0 OR denominator >= 'Infinity'::numeric
               OR numerator >= 'Infinity'::numeric
               OR public.t145_percent_valid(value) IS NOT TRUE THEN
                RETURN NULL;
            END IF;
            IF numerator < 0 THEN
                RETURN 1;
            END IF;
            IF numerator > 100 * denominator THEN
                RETURN -1;
            END IF;

            integer_part := split_part(value, '.', 1)::integer;
            fractional_part := CASE
                WHEN strpos(value, '.') = 0 THEN ''
                ELSE split_part(value, '.', 2)
            END;
            rational_integer := div(numerator, denominator);
            remainder := mod(numerator, denominator);
            IF integer_part::numeric < rational_integer THEN
                RETURN -1;
            ELSIF integer_part::numeric > rational_integer THEN
                RETURN 1;
            END IF;

            -- The remainder is below the denominator; avoid an overflowing
            -- remainder * 10^32 near NUMERIC's 131072-integer-digit boundary.
            chunk_size := least(32, 131072 - length(trunc(denominator)::text));
            IF chunk_size < 1 THEN
                RETURN NULL;
            END IF;
            WHILE digit_offset <= length(fractional_part) LOOP
                chunk_length := least(chunk_size, length(fractional_part) - digit_offset + 1);
                scale := power(10::numeric, chunk_length);
                remainder := remainder * scale;
                rational_chunk := div(remainder, denominator);
                remainder := mod(remainder, denominator);
                supplied_chunk := substr(fractional_part, digit_offset, chunk_length)::numeric;
                IF supplied_chunk < rational_chunk THEN
                    RETURN -1;
                ELSIF supplied_chunk > rational_chunk THEN
                    RETURN 1;
                END IF;
                digit_offset := digit_offset + chunk_length;
            END LOOP;
            IF remainder = 0 THEN
                RETURN 0;
            END IF;
            RETURN -1;
        END $$;

        CREATE OR REPLACE FUNCTION public.t145_percent_net_matches(
            base numeric, net numeric, value text)
        RETURNS boolean
        LANGUAGE plpgsql IMMUTABLE PARALLEL SAFE STRICT
        SET search_path = pg_catalog AS $$
        DECLARE
            denominator numeric;
            discount_amount numeric;
            lower_numerator numeric;
            upper_numerator numeric;
        BEGIN
            IF base < 0 OR base >= 'Infinity'::numeric OR trunc(base, 2) <> base
               OR net < 0 OR net >= 'Infinity'::numeric OR trunc(net, 2) <> net
               OR net > base OR public.t145_percent_valid(value) IS NOT TRUE THEN
                RETURN false;
            END IF;
            IF base = 0 THEN
                RETURN net = 0;
            END IF;
            -- Half-up cent rounding: (discount - .005)*100/base < percent
            -- <= (discount + .005)*100/base. Multiply by an exact decimal
            -- before comparing: numeric division can round the chosen scale.
            denominator := base * 0.01;
            discount_amount := base - net;
            lower_numerator := discount_amount - 0.005;
            upper_numerator := discount_amount + 0.005;
            RETURN public.t145_decimal_compare_rational(
                       value, lower_numerator, denominator) > 0
               AND public.t145_decimal_compare_rational(
                       value, upper_numerator, denominator) <= 0;
        END $$;
    """)


def downgrade() -> None:
    raise RuntimeError("T173 NUMERIC compatibility migration is forward-only")
