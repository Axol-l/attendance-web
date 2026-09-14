"""
config 包初始化文件
配置 PyMySQL 作为 MySQLdb 的替代品

⚠️ 必须在任何 Django 数据库访问发生之前执行 install_as_MySQLdb()，
   因此这里放在包初始化里，而不是 settings 里。settings 是被本包导入的。
"""
import pymysql

# 使用 PyMySQL 替代 MySQLdb
pymysql.install_as_MySQLdb()
