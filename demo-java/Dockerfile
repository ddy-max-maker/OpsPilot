# OpsPilot Next D3 Java Spring Boot 演示服务（多阶段构建：Maven → JRE）
FROM maven:3.9-eclipse-temurin-17 AS build
WORKDIR /src
COPY settings.xml /root/.m2/settings.xml
COPY pom.xml .
RUN mvn -B dependency:go-offline || true
COPY src ./src
RUN mvn -B package -DskipTests

FROM eclipse-temurin:17-jre
WORKDIR /app
COPY --from=build /src/target/demo-java-*.jar /app/app.jar
EXPOSE 8080
ENTRYPOINT ["java", "-jar", "/app/app.jar"]
