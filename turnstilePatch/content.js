// Turnstile Patch - 隐藏自动化标识，加速 Turnstile 验证
// 在 document_start 阶段执行，确保在页面脚本之前生效

(function () {
    "use strict";

    // 1. 隐藏 navigator.webdriver 标识
    // Chrome 自动化模式下 navigator.webdriver = true，Turnstile 会检测此属性
    try {
        Object.defineProperty(navigator, "webdriver", {
            get: function () {
                return false;
            },
            configurable: true,
        });
    } catch (e) {}

    // 2. 移除 Chrome 自动化相关的 Runtime 属性
    try {
        if (window.chrome && window.chrome.runtime) {
            delete window.chrome.runtime.onConnect;
            delete window.chrome.runtime.onMessage;
        }
    } catch (e) {}

    // 3. 覆盖 permissions.query，隐藏 notifications 权限异常
    try {
        var origQuery = navigator.permissions.query.bind(navigator.permissions);
        navigator.permissions.query = function (params) {
            if (params.name === "notifications") {
                return Promise.resolve({ state: Notification.permission });
            }
            return origQuery(params);
        };
    } catch (e) {}

    // 4. 修补 plugin 数量，模拟正常浏览器
    try {
        Object.defineProperty(navigator, "plugins", {
            get: function () {
                return [1, 2, 3, 4, 5];
            },
            configurable: true,
        });
    } catch (e) {}

    // 5. 修补 languages 属性
    try {
        Object.defineProperty(navigator, "languages", {
            get: function () {
                return ["en-US", "en"];
            },
            configurable: true,
        });
    } catch (e) {}

    // 6. 页面加载完成后，自动监控并点击 Turnstile 复选框
    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", autoClickTurnstile);
    } else {
        autoClickTurnstile();
    }

    function autoClickTurnstile() {
        // Cross-origin: cannot open iframe DOM. Click the iframe box from the parent page.
        var checkCount = 0;
        var maxChecks = 120; // ~60s
        var lastClickAt = 0;
        var timer = setInterval(function () {
            checkCount++;
            if (checkCount > maxChecks) {
                clearInterval(timer);
                return;
            }
            try {
                if (
                    window.turnstile &&
                    typeof window.turnstile.getResponse === "function"
                ) {
                    var resp = window.turnstile.getResponse();
                    if (resp && String(resp).length >= 80) {
                        clearInterval(timer);
                        return;
                    }
                }
                var now = Date.now();
                if (now - lastClickAt < 1500) return;

                var nodes = document.querySelectorAll(
                    'iframe[src*="challenges.cloudflare.com"], iframe[src*="turnstile"], #xai-force-ts-host iframe, .cf-turnstile iframe, .cf-turnstile'
                );
                for (var i = 0; i < nodes.length; i++) {
                    var el = nodes[i];
                    var r = el.getBoundingClientRect();
                    if (r.width < 20 || r.height < 20) continue;
                    // Checkbox sits on the left of the managed widget.
                    var x = r.left + Math.min(28, Math.max(18, r.width * 0.1));
                    var y = r.top + r.height * 0.5;
                    var target = document.elementFromPoint(x, y) || el;
                    ["mouseover", "mouseenter", "mousemove", "mousedown", "mouseup", "click"].forEach(function (type) {
                        try {
                            target.dispatchEvent(
                                new MouseEvent(type, {
                                    bubbles: true,
                                    cancelable: true,
                                    view: window,
                                    clientX: x,
                                    clientY: y,
                                    button: 0,
                                })
                            );
                        } catch (e) {}
                    });
                    lastClickAt = now;
                    break;
                }
            } catch (e) {}
        }, 500);
    }
})();
