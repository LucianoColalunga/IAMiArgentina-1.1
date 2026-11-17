-- Recifrado en segundo plano después de rotar DATA_ENCRYPTION_KEYS (persistence/rotation.py).
-- Una fila por tabla con datos cifrados: con qué clave se recifra, desde cuándo, hasta qué clave
-- primaria llegó la vuelta en curso y cuántos datos recifró. Si el mantenimiento se reinicia,
-- sigue desde donde estaba.
CREATE TABLE key_rotation (
    target           text PRIMARY KEY,                    -- contacts, messages, oauth_tokens
    key_id           text NOT NULL,                       -- clave activa con la que se recifra
    started_at       timestamptz NOT NULL DEFAULT now(),  -- cuándo se vio esa clave por primera vez
    pass_started_at  timestamptz NOT NULL DEFAULT now(),  -- inicio de la vuelta en curso (o de la próxima)
    last_id          text,                                -- última clave primaria recorrida en la vuelta
    rotated          bigint NOT NULL DEFAULT 0,           -- datos recifrados en la vuelta
    failed           bigint NOT NULL DEFAULT 0,           -- datos que no se pudieron descifrar
    finished_at      timestamptz,
    updated_at       timestamptz NOT NULL DEFAULT now()
);
