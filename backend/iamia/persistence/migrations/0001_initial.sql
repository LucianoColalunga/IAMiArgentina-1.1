-- Esquema inicial de IAMiArgentina 1.1.
-- Los datos personales se guardan cifrados en la aplicación (AES-256-GCM) y se buscan por un
-- seudónimo HMAC: con acceso solo a la base no se pueden leer teléfonos, ids ni mensajes.

CREATE EXTENSION IF NOT EXISTS vector;

-- Persona: agrupa contactos de distintos canales SOLO si la persona lo pidió (/vincular).
CREATE TABLE persons (
    id          uuid PRIMARY KEY,
    created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE contacts (
    id                  uuid PRIMARY KEY,
    channel             text NOT NULL,
    account_id          text NOT NULL,              -- cuenta propia que recibió el mensaje
    address_hash        text NOT NULL,              -- HMAC(canal, dirección): búsqueda sin el dato
    address_enc         text NOT NULL,              -- dirección cifrada (para poder responder)
    person_id           uuid REFERENCES persons (id) ON DELETE SET NULL,
    locale              text,
    opted_out           boolean NOT NULL DEFAULT false,
    erase_requested_at  timestamptz,
    created_at          timestamptz NOT NULL DEFAULT now(),
    last_seen_at        timestamptz NOT NULL DEFAULT now(),
    last_inbound_at     timestamptz,
    CONSTRAINT contacts_channel_address UNIQUE (channel, address_hash)
);
CREATE INDEX contacts_person ON contacts (person_id) WHERE person_id IS NOT NULL;
CREATE INDEX contacts_last_seen ON contacts (last_seen_at);
CREATE INDEX contacts_erase ON contacts (erase_requested_at) WHERE erase_requested_at IS NOT NULL;

CREATE TABLE conversations (
    id                uuid PRIMARY KEY,
    contact_id        uuid NOT NULL REFERENCES contacts (id) ON DELETE CASCADE,
    channel           text NOT NULL,
    state             text NOT NULL DEFAULT 'bot'
                      CHECK (state IN ('bot', 'waiting_human', 'human', 'closed')),
    assigned_to       text,                         -- "sub" OIDC del operador
    no_info_count     integer NOT NULL DEFAULT 0,
    handoff_reason    text,
    handoff_at        timestamptz,                  -- cuándo pidió hablar con una persona
    thread_ref        text,                         -- hilo de la plataforma (correo, X)
    subject           text,
    opened_at         timestamptz NOT NULL DEFAULT now(),
    last_activity_at  timestamptz NOT NULL DEFAULT now(),
    closed_at         timestamptz,
    close_reason      text
);
-- Una sola conversación abierta por contacto.
CREATE UNIQUE INDEX conversations_one_open ON conversations (contact_id) WHERE state <> 'closed';
CREATE INDEX conversations_queue ON conversations (state, last_activity_at) WHERE state IN ('waiting_human', 'human');
CREATE INDEX conversations_contact ON conversations (contact_id, opened_at DESC);

-- Mensajes entrantes y salientes. La tabla también es la bandeja de salida transaccional:
-- un mensaje se guarda antes de publicarse en la cola y el sweeper republica lo que quedó sin
-- publicar o con el lease vencido. Así no se pierde nada si la cola o un worker se caen.
CREATE TABLE messages (
    id                   uuid PRIMARY KEY,
    seq                  bigint GENERATED ALWAYS AS IDENTITY,
    conversation_id      uuid NOT NULL REFERENCES conversations (id) ON DELETE CASCADE,
    contact_id           uuid NOT NULL REFERENCES contacts (id) ON DELETE CASCADE,
    channel              text NOT NULL,
    direction            text NOT NULL CHECK (direction IN ('in', 'out')),
    author               text NOT NULL CHECK (author IN ('user', 'bot', 'operator', 'system')),
    kind                 text NOT NULL,
    body_enc             text,                      -- texto cifrado, con datos personales enmascarados
    provider_message_id  text,
    reply_to_id          uuid REFERENCES messages (id) ON DELETE SET NULL,
    status               text NOT NULL CHECK (status IN (
                             'received', 'processing', 'answered', 'ignored',
                             'pending', 'sending', 'sent', 'delivered', 'read', 'failed', 'dead')),
    attempts             integer NOT NULL DEFAULT 0,
    enqueued_at          timestamptz,
    lease_until          timestamptz,
    operator             text,
    outcome              text,
    cache_layer          text,
    kb_version           text,
    latency_ms           integer,
    error_code           text,
    error_detail         text,
    sent_at              timestamptz,
    created_at           timestamptz NOT NULL DEFAULT now(),
    updated_at           timestamptz NOT NULL DEFAULT now(),
    meta                 jsonb NOT NULL DEFAULT '{}'::jsonb
);
-- Idempotencia de entrada: la plataforma reintenta y el mismo mensaje no se procesa dos veces.
CREATE UNIQUE INDEX messages_inbound_dedup ON messages (channel, provider_message_id) WHERE direction = 'in';
CREATE INDEX messages_outbound_provider ON messages (channel, provider_message_id)
    WHERE direction = 'out' AND provider_message_id IS NOT NULL;
CREATE INDEX messages_conversation ON messages (conversation_id, seq);
CREATE INDEX messages_unpublished ON messages (created_at) WHERE status IN ('received', 'pending');
CREATE INDEX messages_leased ON messages (lease_until) WHERE status IN ('processing', 'sending');
CREATE INDEX messages_problems ON messages (updated_at DESC) WHERE status IN ('failed', 'dead');
CREATE INDEX messages_created ON messages (created_at);

CREATE TABLE audit_log (
    id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    at           timestamptz NOT NULL DEFAULT now(),
    actor        text NOT NULL,                     -- 'system' o "sub" del operador
    action       text NOT NULL,
    target_type  text,
    target_id    text,
    detail       jsonb NOT NULL DEFAULT '{}'::jsonb  -- nunca datos personales
);
CREATE INDEX audit_log_at ON audit_log (at);

CREATE TABLE link_codes (
    code_hash   text PRIMARY KEY,
    contact_id  uuid NOT NULL REFERENCES contacts (id) ON DELETE CASCADE,
    expires_at  timestamptz NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now()
);

-- Tokens OAuth de las plataformas que los renuevan (X), cifrados.
CREATE TABLE oauth_tokens (
    provider           text PRIMARY KEY,
    access_token_enc   text NOT NULL,
    refresh_token_enc  text NOT NULL,
    expires_at         timestamptz NOT NULL,
    updated_at         timestamptz NOT NULL DEFAULT now()
);

-- Base de conocimiento azul/verde: cada ingesta crea una tabla kb_chunks_<versión> con su índice
-- HNSW, se valida y recién entonces se activa cambiando un puntero. Las consultas en curso no se
-- interrumpen y volver a la versión anterior es inmediato.
CREATE TABLE kb_versions (
    version        text PRIMARY KEY,
    table_name     text NOT NULL UNIQUE,
    embed_model    text NOT NULL,
    dimensions     integer NOT NULL,
    documents      integer NOT NULL DEFAULT 0,
    chunks         integer NOT NULL DEFAULT 0,
    source_digest  text,
    status         text NOT NULL DEFAULT 'building'
                   CHECK (status IN ('building', 'ready', 'active', 'retired', 'failed')),
    created_at     timestamptz NOT NULL DEFAULT now(),
    activated_at   timestamptz
);

CREATE TABLE kb_active (
    singleton   boolean PRIMARY KEY DEFAULT true CHECK (singleton),
    version     text NOT NULL REFERENCES kb_versions (version),
    updated_at  timestamptz NOT NULL DEFAULT now()
);
