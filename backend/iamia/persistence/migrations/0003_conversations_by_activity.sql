-- La consola lista las conversaciones de cada estado por última actividad: la cola de atención de
-- la más vieja a la más nueva, las del asistente y las cerradas al revés. Con millones de
-- conversaciones, sin un índice para todos los estados cada consulta ordenaría la tabla entera.
-- Este índice reemplaza al parcial que cubría solo la cola de atención.
CREATE INDEX conversations_state_activity ON conversations (state, last_activity_at);
DROP INDEX conversations_queue;
