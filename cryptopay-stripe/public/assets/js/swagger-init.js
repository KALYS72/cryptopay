// Запуск Swagger UI на странице /docs. Отдельный файл, а не inline-скрипт:
// строгая политика безопасности (CSP) запрещает скрипты внутри HTML.
window.ui = SwaggerUIBundle({
  url: '/api/openapi.json',
  dom_id: '#swagger-ui',
  deepLinking: true,
  presets: [SwaggerUIBundle.presets.apis],
  layout: 'BaseLayout',
});
