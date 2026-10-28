#!/bin/sh
# M1 故障注入（MySQL 连接负载 + 慢查询）：由 faults.ps1 经 docker exec 调用
# 用法：inject [connections] / recover
ACTION="${1:-inject}"
CONNS="${2:-40}"
ROOT_PW="$MYSQL_ROOT_PASSWORD"

if [ "$ACTION" = "recover" ]; then
  for id in $(mysql -uroot -p"$ROOT_PW" -N -e "SELECT id FROM information_schema.processlist WHERE info LIKE '%SLEEP(%' OR info LIKE '%BENCHMARK%'"); do
    mysql -uroot -p"$ROOT_PW" -e "KILL $id" 2>/dev/null
  done
  echo "[M1] sessions killed"
  exit 0
fi

i=0
while [ "$i" -lt "$CONNS" ]; do
  mysql -uroot -p"$ROOT_PW" -e "SELECT SLEEP(180);" &
  i=$((i+1))
done
sleep 2
for j in 1 2 3 4 5; do
  mysql -uroot -p"$ROOT_PW" -e "SELECT BENCHMARK(3000000, MD5('slow-demo-query'));"
  sleep 1
done
wait
