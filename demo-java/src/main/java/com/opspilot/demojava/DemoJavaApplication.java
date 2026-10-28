package com.opspilot.demojava;

import org.springframework.boot.SpringApplication;
import org.springframework.boot.autoconfigure.SpringBootApplication;

/**
 * OpsPilot Next D3 演示：Java Spring Boot 可观测服务。
 *
 * <p>观测面：/actuator/health（健康）、/actuator/prometheus（指标，被 Prometheus 真实抓取）、
 * OTLP 链路导出至 Jaeger、控制台日志（被 docker logs 采集）。
 * 故障注入经 POST /__faults（真实行为注入，非 Mock 返回字符串）。
 */
@SpringBootApplication
public class DemoJavaApplication {
    public static void main(String[] args) {
        SpringApplication.run(DemoJavaApplication.class, args);
    }
}
