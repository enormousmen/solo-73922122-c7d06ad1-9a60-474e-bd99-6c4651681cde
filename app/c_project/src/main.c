/*
 * main.c - 诊断工具入口（源文件 1/2）
 * 依赖链：main.c -> diag.h -> diag_util.h -> diag_config.h
 */
#include <stdio.h>
#include "diag.h"

int main(void)
{
    diag_sensor_t sensors[DIAG_MAX_SENSORS] = {
        { 0, 42 },
        { 1, 77 },
        { 2, 91 },
        { 3, 65 },
    };

    printf("%s %s\n", DIAG_TOOL_NAME, DIAG_VERSION);
    int critical = diag_run(sensors, 4);
    printf("critical readings: %d\n", critical);
    return critical > 0 ? 0 : 1;
}
