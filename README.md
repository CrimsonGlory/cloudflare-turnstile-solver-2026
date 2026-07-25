# rust-cloudflare-turnstile-solver-2026

A proof-of-concept Cloudflare Turnstile bypass system built in Rust. Includes a token harvesting mechanism comprising a widget generator, a proxy managing extension, a Turnstile checkbox clicker, and a token server for receiving and managing solved tokens. No API service required. 

---

### Pros

| Major |
| :--- |
| No API service is required. This is completely free of charge to use. |
| Solver can quickly generate tokens. |
| The method is relatively firm and not as easy to patch as other bypasses, as it relies on overriding pages to avoid any policies like CORs or any fingerprinting, and the checkbox identifier will work as long as Cloudflare does not drastically change the UI of the widget itself. |
| This method has a far higher success rate than many other methods. |
| Because this method uses standard web browsers, the entire solving process comes off as legitimate to Cloudflare. |
| Great for building headless applications. Even though the solver itself needs GUI, once the token is solved for you can do everything headlessly. |

| Minor |
| :--- |
| Data is handled and already managed by a server that makes managing your haverested tokens easy. |
| Method is generally effective when you know the website you want to apply it to beforehand. |
| Easy to use and setup, especially compared to certain other bypasses. |

### Caveats

| Major |
| :--- |
| The solver is **not headless** — a GUI is required. |
| Ineffective for general, random web-scraping. Knowing the websites it will be used on is most effective. |
| No custom fingerprint spoofing for TLS/JA4, canvas, and other metrics like navigator values. But, given the legitimacy of the browsers, this isn't as severe as usual. |

| Minor |
| :--- |
| The method relies on a browser with overrides enabled. |
| Designed for smaller-scale token harvesting, though the token server architecture does support larger-scale operations. |
| Tunneling multiple proxies through each iframe is not supported. Do note this may potentially be added in the future if a feasible solution (some form of advanced tunneling) is found. Note that per-window proxying, however, is supported. |

---

## Supported Browsers (as of now).

To help create fingerprint variation, the goal of this system is to support multiple browsers (i.e. they have a proxy connector extension).

Current browsers that are supported (as a note right now it's only FireFox, I plan to add support for CDP browsers very soon). 

- FireFox. You can spawn as many pages as you want. Proxies are per tab, not linked to account.

---

## Components

The bypass is comprised of four main components:

1. **Token Harvester / Turnstile Widget Loader**
2. **Turnstile Widget Identifier & Clicker**
3. **Token Server**
4. **Proxy Extensions**

---

### 1. Token Harvester / Turnstile Widget Loader

The Token Harvester loads the Turnstile widget by spawning multiple iframe-based solvers, each pointing at a different Cloudflare site widget. Every solver iframe connects to the token server and forwards any solved tokens to it, which is done once it recieves an on demand request from your backend/recievers.

**Setup:**

1. **Configure the files.** The config is in `index.html`:
   - Set `PRELOAD_IFRAMES` (the number of iframe solvers to load on page start) **NOTE: PLEASE KEEP PRELOAD_IFRAMES AT 1. Currently, upon any solve the location reloads. Additionally, multi-iframe solving on a single page has been found to be quite slow. This is all but deprecated as of now but if a feasible solution to tunneling is implemented (as discussed in future plans) it may become useful again.**, `TOKEN_SERVER_HOST` (your token server host, obviously), `PROXY_CONNECT_TIMEOUT` (time for proxy connection to timeout and page to begin reloading), and `USE_PROXY_SOLVING` (boolean to determine if you want to use the multi-proxy solving system). Originally I did just use const SITEKEY which is why that's still declared in the index.html, but after having to change it around consistently it got annoying. So it's set in localStorage now. So set `localStorage.sitekey` (the website's Cloudflare sitekey) in localStorage.
  
   - If you do not know how to access a sitekey, here is a short and easy method you can use to access it: in devtools, find the turnstile.js file in the sources tab. In it, ctrl f "sitekey". You'll see many instances. You can breakpoint a few of these and then run the page to get into the scope, which will have the sitekey. 

2. **Set your proxies.**  Set your linesplit list of proxies to `localStorage.proxies`. The proxy extension will connect to a proxy from this list according to the recieved solver idx. Note the proxies list should include the protocol extension protocol://

3. **Apply as browser overrides.** Replace the target webpage's main HTML file with `index.html`. 

**Why overrides?**

Using overrides does require loading the actual page, but it sidesteps issues with CORS policies, TLS fingerprinting, and other browser/address analysis the target site may employ. Because the page loads normally and passes all standard security checks, our modified scripts can generate tokens cleanly without triggering those protections.

---

### 2. Turnstile Clicker

The Turnstile Clicker automatically solves checkbox click challenges. Run the relevant `main.rs` file to start it. The clicker is **disabled by default** — press **F8** to toggle it on or off.

**Setup:**

Set the config values described in `main.rs`. That's all.
**How it works:**

The clicker identifies Cloudflare Turnstile checkboxes by analyzing pixel RGB values. It searches for pixels matching the characteristic grey ring border of the Turnstile checkbox. Once a candidate pixel is found, it performs a depth-first search (DFS) to verify the pixel forms a closed ring/loop. It then searches inward from all four sides to isolate the whitespace within the border — the actual clickable area. Finally, it dispatches OS-level input events to move the mouse to a point within that region and click.

> **Note:** The F8 toggle exists just to prevent any potential false positives. Toggle it on when you're on the pages just to avoid false positives (though it is pretty thorough, but just in case).

---

### 3. Token Server

The Token Server doesn't participate in solving--it routes solved requests to available solvers, and forwards completed tokens back to their respective requesters. Solver iframes forward their tokens here as they're solved.

**Setup:**

Set the `PORT` value in config. That's all.

**Packet & Protocol Structure:**

*All values are little-endian.*

*Serverbound (client -> server):*

| Sent From | Header | Description |
|-----------|--------|-------------|
| Solver | `0` | Incoming token result from a solver. The server routes it back to the specific requester who asked for it by extracting the requester id, then re-adds the solver to the available queue. Structure: <0, ...requester_id_bytes (u32), ...solver_idx_bytes (u32), ...token_bytes>. If the solver failed to get a token, then there are no token bytes. |
| Reciever | `1` | On-demand solve request from a requester. The server pulls the next available solver from the queue and forwards this assignment to them. You can specify a specific user-agent in this packet, which will then make the token server force a solver with that user-agent. This is particularly useful for mimicing real web traffic, and distributing solves across an amount that mimics the real web traffic distribution of user-agents. You can also just leave user-agent as "" for a random selection. The `.render` function call for turnstile, which initializes the widget, can take in special fields and extra data, such as `action`, or `cData`. To counter this, you may also specify field data for these in this packet, as shown in the provided structure. These fields will then be passed into the render call the solver makes. Structure: <1, ...solver_idx_bytes (u32), user_agent_len (u8), ...user_agent_bytes ...(field_name_len (u8), ...field_name_bytes, field_value_len (u8), ...field_value_bytes)>. |
| Solver | `2` | Register the sending socket as a solver. The server appends its socket id to the available solvers queue. Additionally, the solver sends its `navigator.userAgent` to the token server. A user-agent is referred to by the solver when making requests, which will force only a solver with the matching user-agent to solve the request. Structure: <2, ...user_agent_bytes>. |
| Reciever | `3` | Request the total available solvers count. Good for analyzing how many active solving instances you can spawn. Structure: <3>. |

*Clientbound (server -> client):*

| Endpoint | Name | Description |
|----------|------|-------------|
| Reciever | Token | Incoming token delivered to a requester. Structure: <...solver_idx_bytes (u32), ...token_bytes>. If the solver failed to get a token, then there are no token bytes. |
| Reciever | Solvers Unavailable | A request made by a solver could not be completed because no solvers were available to accept it. Structure: <0>. |
| Solver | Solve Request | Solve a turnstile widget request that is delivered to a solver. Structure: <...solver_idx_bytes (u32), ...requester_id_bytes (u32), ...(field_name_len (u8), ...field_name_bytes, field_value_len (u8), ...field_value_bytes)>. |
| Reciever | Available Solvers Result | The result to the available solvers count request you made. Note, the zero at the end of this packet is dummy data. It is actually added because I made the accepted parsing system for these packets length based to check packet type, but the token packet will deliver 4 bytes if it fails to recieve a token. I added the extra byte to this packet to solve the length collision because no branching logic is required for this one, and its a much smaller and simpler case so I just prefered it. Structure: <...available_solvers_bytes (u32), 0>. | 

*Note that the clientbound packets do not have headers since each endpoint recieves few, easily disceranble packets. Recievers recieve a packet of only length 1 (Solvers Unavailable), the token packet itself (can be length 4 if there is no token and the request failed), or a packet of length 5 (total available solvers). This makes discerning packets by length easy. The solver can only recieve a solve request.*

---

### 4. Proxy Extensions

The extensions allow us to utilize browser proxy API capabilities to connect to proxies, per tab.

For each browser you'll be using, you'll need to add the respective extension for that browser from `proxy-extensions` to whatever browser you are using (ex. firefox for firefox, cdp for cdp browsers, truly a shocker), and run it. These extensions provide the API necessary for asynchronous proxy connections, allowing you to await and connect to a proxy before continuing execution. 

---

## Bypassing WebRTC

WebRTC can leak your real IP. To solve this issue, here are three solutions you can use:

1. Disable WebRTC features in your browser config, if applicable. For example, in FireFox: in `about:config`, set `media.peerconnection.ice.nohost`, `media.peerconnection.ice.default_address_only`, `media.peerconnection.ice.proxy_only_if_behind_proxy`, and `media.peerconnection.ice.obfuscate_host_addresses` to `true`. These stop WebRTC from peeking at any host candidates and accidently leaking your real IP, but STILL leave WebRTC enabled, which can help minimize bot risk.
2. If you want to fully disable WebRTC (may increase bot risk), you can also just do this. For example, in FireFox, you can set `media.peerconnection.enabled` to `false`.
3. Simply download any anti WebRTC extension (there are many anti WebRTC extensions that exist). These may disable certain WebRTC features or disable WebRTC fully. Be cautious, as again these can increase your risk of being flagged.

---

## Starting It Up

1. Start the **token server**.
2. Start your backend, token managing and requesting system. 
3. Start the **auto-clicker**.
4. Open your **modified webpages**.
5. Press **F8** to enable the auto-clicker.
6. Watch it go.

---

## Some Helpers for your Backend

Your backend that actually gets and requests solves for tokens will need to interact with the token server. 

You will need a reference to a proxies txt list. This list should match the one you set at localStorage.proxies on the solver page.

For any turnstile render call custom fields, such as "cData" or "action" as previously mentioned, you'll need to figure out how they are generated for your target, and recreate the logic to how these fields are generated so that you can pass them into your solve request packet. "action" is usually a hardcoded string, but "cData" is often used as an individual ID/verification field. In short, ensure all fields of the turnstile render call match.

**Construct solve request packet:**

```
// proxy_idx = literally just the index of your proxy in the proxy list.
// user_agent = user-agent string of the target you want to run (matches to navigator.userAgent). 
// fields = object, { name: value, name2: value2, ... namen: valuen }. Names and values are strings.
function construct_solver_request_packet(proxy_idx, user_agent = "", fields = {}) {
   let encoder = new TextEncoder();
   let packet = Array(5);
   packet[0] = 1;
   packet[1] = proxy_idx & 255;
   packet[2] = (proxy_idx >> 8) & 255;
   packet[3] = (proxy_idx >> 16) & 255;
   packet[4] = (proxy_idx >> 24) & 255;
   let user_agent_bytes = encoder.encode(user_agent);
   packet[5] = user_agent_bytes.length;
   packet.push(...user_agent_bytes);
   for (let field_name in fields) {
         let field_value = fields[field_name];
         let field_name_bytes = encoder.encode(field_name);
         let field_value_bytes = encoder.encode(field_value);
         let field_name_len = field_name_bytes.length;
         let field_value_len = field_value_bytes.length;
         packet.push(field_name_len);
         packet.push(...field_name_bytes);
         packet.push(field_value_len);
         packet.push(...field_value_bytes);
   }
   return new Uint8Array(packet);
};
```

**Parse token response packet:**

```
// packet = packet buffer.
function parse_token_response_packet(packet) {
    let view = new DataView(packet);
    let solver_idx = view.getUint32(0, true);
    let token = undefined;
    if (packet.length > 4) {
      let u8 = new Uint8Array(packet);
      token = new TextDecoder().decode(u8.subarray(4));
    }
    return [solver_idx, token];
};
```

**Parse available solvers packet:**

```
// packet = packet buffer.
function parse_available_solvers_count_packet(packet) {
    let view = new DataView(packet);
    return [view.getUint32(0, true)];
};
```

**Match packets:**

```
// packet = packet buffer
if (packet.byteLength > 5) {
   // Token Packet
} else if (packet.byteLength == 5) {
   // Available Solvers Result
} else if (packet.byteLength == 4) {
   // Failed Token Result (only solver idx is sent back)
} else {
   // Solvers Unavailable
}
```

---

## Future Plans/What this Needs (may not be done, but if major updates do occur to this project it will likely be these).

Custom navigator, webgl debug renderer, and window dimensions spoofing.

An automatic page-loader and harvester setup script may be created in order to aid with multi-proxy solving, as per page loads are currently needed for such.

If a feasible solution is found, a way to tunnel individual iframes (hence enhancing multi-proxy solving outside of just different tabs) may be implemented.

---

## Contributing

All contributions are very welcome. If you have a way to improve this project, please share with issues, pull requests, etc.

---

## Some Images and Media of Applications

<img width="1919" height="942" alt="image" src="https://github.com/user-attachments/assets/bd5b88a4-b824-4591-832e-812e254adb68" />
https://github.com/user-attachments/assets/dfca651a-e13c-47f7-8d54-80b029a4983b
<img width="1919" height="1001" alt="image" src="https://github.com/user-attachments/assets/48ce3a79-d111-4838-b845-88c62b2144f8" />
