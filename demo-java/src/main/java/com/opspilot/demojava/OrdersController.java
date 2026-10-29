package com.opspilot.demojava;

import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.time.Duration;
import java.util.LinkedHashMap;
import java.util.Map;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.concurrent.atomic.AtomicLong;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RestController;

/**
 * 业务与故障注入控制器（D3 跨语言诊断演示）。
 *
 * <p>故障场景（全部为真实行为注入，服务真实执行慢/真抛错/真调依赖）：
 * <ul>
 *   <li>J1 接口超时：delay_ms &gt; 0 时业务接口真实 sleep（被业务线程占用）；</li>
 *   <li>J2 异常响应：error_rate% 概率返回 500（含真实异常日志）；</li>
 *   <li>J3 依赖故障：dependency_url 非空时业务接口真实调用该依赖（如 demo-app），
 *       依赖不可达时返回 503 并记录连接异常日志。</li>
 * </ul>
 */
@RestController
public class OrdersController {
    private static final Logger log = LoggerFactory.getLogger(OrdersController.class);

    private final AtomicLong delayMs = new AtomicLong(0);
    private final AtomicInteger errorRate = new AtomicInteger(0);
    private volatile String dependencyUrl = "";
    private final HttpClient httpClient =
            HttpClient.newBuilder().connectTimeout(Duration.ofSeconds(3)).build();

    public static class FaultConfig {
        public long delayMs;
        public int errorRate;
        public String dependencyUrl;
    }

    @PostMapping("/__faults")
    public Map<String, Object> setFaults(@RequestBody FaultConfig cfg) {
        if (cfg.delayMs >= 0) {
            delayMs.set(cfg.delayMs);
        }
        if (cfg.errorRate >= 0) {
            errorRate.set(Math.min(100, cfg.errorRate));
        }
        if (cfg.dependencyUrl != null) {
            dependencyUrl = cfg.dependencyUrl;
        }
        log.warn("[fault] updated: delayMs={}, errorRate={}%, dependencyUrl='{}'",
                delayMs.get(), errorRate.get(), dependencyUrl);
        return currentFaults();
    }

    @GetMapping("/__faults")
    public Map<String, Object> currentFaults() {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("delay_ms", delayMs.get());
        m.put("error_rate", errorRate.get());
        m.put("dependency_url", dependencyUrl);
        return m;
    }

    /** 业务接口：订单查询（注入点：延迟/异常/依赖故障）。 */
    @GetMapping("/api/orders/{id}")
    public ResponseEntity<Map<String, Object>> getOrder(@PathVariable String id)
            throws InterruptedException {
        long started = System.currentTimeMillis();
        long delay = delayMs.get();
        if (delay > 0) {
            Thread.sleep(delay); // J1：真实耗时
        }
        int rate = errorRate.get();
        if (rate > 0 && (int) (Math.random() * 100) < rate) {
            log.error("[order] simulated failure for order={} (error_rate={}%)", id, rate);
            throw new IllegalStateException("simulated internal failure (J2 injection)");
        }
        if (!dependencyUrl.isBlank()) {
            try {
                HttpRequest req =
                        HttpRequest.newBuilder(URI.create(dependencyUrl))
                                .timeout(Duration.ofSeconds(3))
                                .GET()
                                .build();
                HttpResponse<String> resp =
                        httpClient.send(req, HttpResponse.BodyHandlers.ofString());
                if (resp.statusCode() >= 500) {
                    return dependencyFailure(id, "dependency returned " + resp.statusCode());
                }
            } catch (Exception e) {
                // J3：依赖不可达（连接被拒/超时）——真实异常传播 + 真实 503 状态码
                log.error("[order] dependency call failed for order={}: {}", id, e.toString());
                return dependencyFailure(id, e.getClass().getSimpleName());
            }
        }
        long cost = System.currentTimeMillis() - started;
        log.info("[order] served order={} in {}ms", id, cost);
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("order_id", id);
        m.put("status", "ok");
        m.put("cost_ms", cost);
        m.put("language", "java");
        return ResponseEntity.ok(m);
    }

    private ResponseEntity<Map<String, Object>> dependencyFailure(String id, String reason) {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("order_id", id);
        m.put("status", "unavailable");
        m.put("error", "dependency_unavailable");
        m.put("reason", reason);
        return ResponseEntity.status(503).body(m);
    }

    /** 慢接口（J1 演示辅助：恒定延迟业务端点）。 */
    @GetMapping("/api/slow")
    public Map<String, Object> slow() throws InterruptedException {
        long delay = delayMs.get();
        if (delay > 0) {
            Thread.sleep(delay);
        }
        return Map.of("status", "ok", "delay_ms", delay);
    }
}
