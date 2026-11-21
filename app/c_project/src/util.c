/*
 * util.c - 诊断工具工具函数（源文件 2/2）
 * 依赖链：util.c -> diag_util.h -> diag_config.h
 */
#include <stdio.h>
#include <string.h>
#include "diag_util.h"

int diag_is_critical(int value)
{
    return value >= DIAG_CRITICAL_THRESHOLD;
}

int diag_format_reading(int id, int value, char *buf, int buf_size)
{
    if (buf == NULL || buf_size <= 0) {
        return -1;
    }
    int n = snprintf(buf, (size_t)buf_size, "sensor#%d value=%d", id, value);
    if (n < 0 || n >= buf_size) {
        return -1;
    }
    return n;
}

int diag_run(const diag_sensor_t *sensors, int count)
{
    if (sensors == NULL || count <= 0) {
        return 0;
    }

    int critical = 0;
    char line[DIAG_MAX_READING_LEN];

    for (int i = 0; i < count; i++) {
        if (diag_format_reading(sensors[i].id, sensors[i].value,
                                line, (int)sizeof(line)) < 0) {
            continue;
        }
        if (diag_is_critical(sensors[i].value)) {
            printf("[CRITICAL] %s\n", line);
            critical++;
        } else {
            printf("[OK]       %s\n", line);
        }
    }
    return critical;
}
