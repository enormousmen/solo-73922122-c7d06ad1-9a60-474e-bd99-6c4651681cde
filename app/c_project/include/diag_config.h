/*
 * diag_config.h - 诊断工具公共配置头（固件工程师会改动的公共头）
 * 被 diag.h 间接嵌套引用，改动它会影响所有源文件的预处理结果。
 */
#ifndef DIAG_CONFIG_H
#define DIAG_CONFIG_H

#define DIAG_TOOL_NAME "c-diag-tool"
#define DIAG_VERSION   "1.0.0"

/* 临界判定阈值（演示：修改此值可观察增量重编行为） */
#define DIAG_CRITICAL_THRESHOLD 80

/* 单条传感器读数的最大长度 */
#define DIAG_MAX_READING_LEN 64

#endif /* DIAG_CONFIG_H */
