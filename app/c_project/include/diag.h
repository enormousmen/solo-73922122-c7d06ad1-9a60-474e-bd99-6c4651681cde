/*
 * diag.h - 诊断工具主头文件（嵌套引用 diag_util.h -> diag_config.h）
 */
#ifndef DIAG_H
#define DIAG_H

#include "diag_util.h"

/* diag_sensor_t 定义在 diag_util.h（经嵌套引用可见） */

/* 对一组传感器读数执行诊断，返回临界读数数量 */
int diag_run(const diag_sensor_t *sensors, int count);

#endif /* DIAG_H */
