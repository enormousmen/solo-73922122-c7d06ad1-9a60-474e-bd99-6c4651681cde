/*
 * diag_util.h - 工具函数声明，嵌套引用公共头 diag_config.h
 */
#ifndef DIAG_UTIL_H
#define DIAG_UTIL_H

#include "diag_config.h"

#define DIAG_MAX_SENSORS 8

typedef struct {
    int id;
    int value;
} diag_sensor_t;

/* 判断读数是否达到临界级别（实现见 util.c） */
int diag_is_critical(int value);

/* 把一条读数格式化为文本，写入 buf，返回写入长度 */
int diag_format_reading(int id, int value, char *buf, int buf_size);

#endif /* DIAG_UTIL_H */
