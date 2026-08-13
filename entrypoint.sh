#!/bin/sh
# Volume-мониторы (Railway, Docker volumes и т.п.) часто монтируют /data
# заново от имени root, даже если образ на этапе сборки уже сделал
# chown bot:bot /data. Поэтому чиним владельца здесь, при каждом старте
# контейнера, ДО того как передать управление непривилегированному
# пользователю bot.
set -e

mkdir -p /data
chown -R bot:bot /data

exec gosu bot "$@"
