// Resolve a configured path to a URL the offscreen document can fetch.
// Absolute Windows/POSIX paths become file:// URLs. http(s), file://, and
// chrome-extension:// values are left alone so Docker can serve config over
// localhost instead of granting the extension file:// access.
function path_to_url(file_path) {
    if (!file_path) return file_path;
    if (/^(file|https?|chrome-extension):\/\//i.test(file_path)) {
        return file_path;
    }
    if (!file_path.startsWith("/") && !/^[A-Za-z]:[\\/]/.test(file_path)) {
        return chrome.runtime.getURL(file_path);
    }
    let normalized = file_path.replace(/\\/g, "/");
    return normalized.startsWith("/") ? "file://" + normalized : "file:///" + normalized;
}

chrome.runtime.onMessage.addListener((message, sender, send_response) => {
    // Read the proxies file.
    if (message.action == "read_proxies_file") {
        let { file_path } = message;
        let file_url = path_to_url(file_path);

        fetch(file_url)
        .then((res) => {
            if (!res.ok) throw new Error("HTTP " + res.status);
            return res.text();
        })
        .then((content) => {
            chrome.runtime.sendMessage({ action: "proxies_file_content", content });
        })
        .catch((err) => {
            chrome.runtime.sendMessage({ action: "proxies_file_content", content: null, error: err.message });
        });

        return false;
    }

    // Read the inject config file text,
    // and send back to the background.
    if (message.action == "read_inject_config_file") {
        let { file_path } = message;
        let file_url = path_to_url(file_path);

        fetch(file_url)
        .then((res) => {
            if (!res.ok) throw new Error("HTTP " + res.status);
            return res.text();
        })
        .then((content) => {
            chrome.runtime.sendMessage({ action: "inject_config_file_content", content });
        })
        .catch((err) => {
            chrome.runtime.sendMessage({ action: "inject_config_file_content", content: null, error: err.message });
        });

        return false;
    }

    if (message.action != "read_override_file") return false;

    let { file_path, request_id, tab_id } = message;
    let file_url = path_to_url(file_path);

    fetch(file_url)
    .then((res) => {
        if (!res.ok) throw new Error("HTTP " + res.status);
        return res.text();
    })
    .then((content) => {
        chrome.runtime.sendMessage({
            action: "override_file_content",
            request_id,
            tab_id,
            content
        });
    })
    .catch((err) => {
        chrome.runtime.sendMessage({
            action: "override_file_content",
            request_id,
            tab_id,
            content: null,
            error: err.message
        });
    });

    return false;
});