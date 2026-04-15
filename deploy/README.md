# Deploy en EC2 (Ubuntu)

## Primera vez: instalar desde cero

```bash
# 1. Conectarte a la EC2
ssh ubuntu@<IP_EC2>

# 2. Clonar el repo (como usuario ubuntu, con tu token o SSH key)
git clone https://github.com/TU_USUARIO/betbot.git
cd betbot

# 3. Correr el setup (no necesita sudo en general, te lo pide solo para systemd)
chmod +x deploy/setup_ec2.sh
./deploy/setup_ec2.sh

# 4. Editar el .env con tu configuración
nano .env

# 5. Probar que funciona con un ciclo manual
./deploy/manage.sh scan-once

# 6. Iniciar el bot en segundo plano
./deploy/manage.sh start
```

---

## Uso diario

```bash
# Ver estado + últimos logs
./deploy/manage.sh status

# Ver logs en tiempo real
./deploy/manage.sh logs

# Ver balance y posiciones abiertas
./deploy/manage.sh balance

# Ver últimas operaciones
./deploy/manage.sh operations

# Detener / Iniciar / Reiniciar
./deploy/manage.sh stop
./deploy/manage.sh start
./deploy/manage.sh restart
```

---

## Actualizar el código

```bash
# Desde la EC2, en la carpeta del repo:
./deploy/manage.sh update
# → hace git pull (como ubuntu, con tu acceso) + pip install + reinicia
```

---

## Cambiar configuración (.env)

```bash
nano .env
./deploy/manage.sh restart
```

---

## Solución al problema de permisos (por qué esto ya no pasa)

El script anterior creaba un usuario `betbot` separado para correr el servicio.
Ese usuario no tenía las SSH keys ni el token de GitHub del usuario `ubuntu`,
entonces `git pull` fallaba.

**Solución**: el servicio ahora corre como el mismo usuario `ubuntu` (o quien sea
que hizo el setup). `git pull` funciona con tus credenciales normales y no hay
confusión de usuarios.

---

## Estructura de archivos

```
deploy/
  setup_ec2.sh          ← instalar todo desde cero (correr una vez)
  manage.sh             ← gestión diaria (start/stop/update/logs...)
  betbot-weather.service ← configuración del servicio systemd
```
