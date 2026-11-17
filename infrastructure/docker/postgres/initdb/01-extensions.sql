-- La migración 0001 también lo hace (CREATE EXTENSION IF NOT EXISTS), pero crearla acá permite
-- que el usuario de la aplicación no necesite privilegios de superusuario.
CREATE EXTENSION IF NOT EXISTS vector;
