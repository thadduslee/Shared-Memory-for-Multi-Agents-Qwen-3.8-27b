#!/usr/bin/env bash
umask 077
cd /home/thaddus/v19-evaluations/v19-info-types-3ep-soclaas-20261104T072550Z
python3 -u supervise.py >> run.log 2>&1 < /dev/null
printf '%s\n' "$?" > exit-code.txt
