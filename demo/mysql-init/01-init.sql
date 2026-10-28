-- OpsPilot Next D3：MySQL 演示库初始化（真实只读诊断账号 + 演示数据）
-- 最小权限只读账号：仅 SELECT（performance_schema 用于慢查询摘要）

CREATE DATABASE IF NOT EXISTS orders_demo;
USE orders_demo;

CREATE TABLE IF NOT EXISTS orders (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    customer VARCHAR(64) NOT NULL,
    amount DECIMAL(10, 2) NOT NULL,
    status VARCHAR(16) NOT NULL DEFAULT 'created',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

INSERT INTO orders (customer, amount, status)
SELECT CONCAT('cust_', n), ROUND(RAND() * 1000, 2), 'created'
FROM (
    SELECT @n := @n + 1 AS n FROM information_schema.columns a, information_schema.columns b LIMIT 200
) seq, (SELECT @n := 0) init;

-- 只读诊断账号（密码为 sandbox 演示值；生产环境必须更换并走 secret 管理）
CREATE USER IF NOT EXISTS 'opspilot_ro'@'%' IDENTIFIED BY 'OpsPilotRo2026!';
GRANT SELECT ON performance_schema.* TO 'opspilot_ro'@'%';
GRANT SELECT ON orders_demo.* TO 'opspilot_ro'@'%';
FLUSH PRIVILEGES;
