# cloudflare-turnstile-solver-2026

A proof-of-concept Cloudflare Turnstile bypass system built with Rust and JavaScript. Includes a token harvesting mechanism comprising a widget generator, a proxy managing extension, a Turnstile checkbox clicker, and a token server for receiving and managing solved tokens. No API service required. 

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
| The solver is **not headless** — a GUI is required. With dockerization this could be fixed. |
| Currently must manually open up tabs, though with dockerization this could also be fixed. |
| Ineffective for general, random web-scraping. Knowing the websites it will be used on is most effective. |
| No custom fingerprint spoofing for TLS/JA4, canvas, and other metrics like navigator values. But, given the legitimacy of the browsers, this isn't as severe as usual. |

| Minor |
| :--- |
| The method relies on a browser with overrides enabled. |
| Designed for smaller-scale token harvesting, though the token server architecture does support larger-scale operations. |
| Tunneling multiple proxies through each iframe is not supported. Do note this may potentially be added in the future if a feasible solution (some form of advanced tunneling) is found. Note that per-window proxying, however, is supported. |

---

## Why use this Method?

This method is designed as a free alternative to more top-level, or "enterprise" grade bypasses. I wanted to avoid solver APIs, and webdriver methods, for a completely real and legit browser instance. 

This also avoids the use of stealth browsers, which work, but constantly require updating all fingerprinting metrics to match current browsers. This is exceptionally high maintenance. I wanted to create a method that also requires much lower maintenance, and will generally be easy to re-implement if patched.

Also, as previously mentioned, this method is particularly most effective when targeting specific websites, as even with an automatic page loader, custom turnstile render field calls, for example, need to be managed per-website. This method is NOT designed as page load -> solve for any website. So it is not viable for webscraping.

What this method IS viable for, though, is solving repeated instances of Cloudflare Turnstiles on a singular site. Though, I have not compared it to standard methods like undetected Selenium loading, so I can't say if it's particularly better or worse, I can say this can safely generate many tokens, while being an extremely simple and free alternative to API services. 

## Supported Browsers (as of now).

To help create fingerprint variation, the goal of this system is to support multiple browsers (i.e. they have a proxy connector extension).

- Any CDP browser (encompasses the vast majority of the web browser market--including Chrome, Edge, Brave, Opera, and more).
- FireFox.

---

## Latency, Throughput, & Overall Effectiveness

This method's primary goal is to token harvest on a specific site. Hence, it's objective is not to just open a site as previously stated. The goal is to generate as many tokens as possible.

This requires the maximization of two metrics:

- Latency (time for a single solver to fully complete the captcha, return token back to requester)
- Throughput (latency scaled by the actual amount of workers that are solving tokens)

### Latency

This project effectively minimizes the latency, or time for a single solver to solve the token. A few things contribute to this fact:
- Because the method is single site harvesting, no page redirect, or entirely new page loading is required.
- The override token solver file strips away unnecessary html elements. You are left with a black background and the widget in in iframe.
- Real browser fingerprints result in short challenges that take only seconds to go through. The time for a Cloudflare Turnstile challenge to go through takes only a few seconds at most. In general, you'll see results of even under two seconds.
- Pipeline of token transfer through solver -> token server -> reciever/backend is extremely fast.
- Vice versa, pipeline of request to solve through reciever -> token server -> solver is also extremely fast.
- Effectively, the approximate latency for a single solver to get a token to a reciever is:
T_solver_recieve_challenge + T_solver_load_widget + T_solver_solve_widget + T_to_bounce_back_to_reciever ~ T_solver_solve_widget (time to solve widget takes a good few seconds, time to do everything else is only a fraction of a second). Now, this value can vary quite a bit. It usually takes at most five seconds but it honestly depends, after a while it may start to slow down too if Cloudflare begins to recognize an attack pattern. No hard specifics on this. It is simply bottlenecked by the cloudflare challenge itself, which not much can be done about. 

This is one of the most effective latencies possible, as it is effectively limited by the time it takes the browser to actually complete the Cloudflare challenge. The only way to even improve the speed on such would be a truly headless, full interaction scheme that could interaction with the turnstile challenge API fully, which is obviously not a feasible method as it would quickly break without much maintenance.

### Throughput

Throughput on this project is also great. In particular, you can easily spawn multiple browsers, including different browsers (as long as they are supported), and because the solve time for a single token usually is under two seconds, even if you spawn, say, only six browsers (a setup I have used is Chrome + Edge + FireFox + Brave + Opera + Opera GX for example), this can easily rack up to a hundreds of tokens in only a short timeframe. 

Note that this is also on a singular device. If expanded to multiple devices (since the system relies on the token server, this can easily be done), this throughput only increases.

The only issue regarding throughput right now, and also mass automation, is this system's requirement of manually loading tabs to solve this. The pixel checkbox click detection makes it so that browser tabs cannot overlap and block each others UIs. This does effectively limit the number of solvers you can have as browser dimensions depend on this. That said, you can still easily fit in a large amount of browsers. You can still easily solve for many tokens with this. Also, though not ideal, you could click on each individual browser that has its checkbox covered when needed to make it render at the top level, so that the checkbox on it becomes visible. This would allow you to use more browsers even with overlapping, though obviously that is a very scuffed solution.

As previously mentioned, dockering this method could solve this issue. This was originally intended as a PoC but turned out to be extremely effective, so I have thought about doing this. I am relatively busy though, but if I have time in the future and want to do this, I may work on dockering this project. This would allow for both headless and also running many more tabs without UI overlap issues. This would effectively maximize the throughput possible with this method.

Still, as explained, throughput is very high, particularly due to the extremely low latency combined with the fact you can still easily get multiple solvers up. 

### Overall

This method is extremely effective when it comes to token harvesting. Even without its maximum dockerized potential, it can still effectively generate hundreds of tokens in only minutes. I will add some direct benchmarks soon along with media and videos of application.

---

## Tested Use Applications

While actively connecting high traffic game sockets for every token solved, this method was used to connect **200** bots to a web game protected by Cloudflare Turnstile in **under 5 minutes (approximately 4 minutes, 40 seconds)**. This only used **five solvers** (Chrome, Edge, Firefox, Opera, Brave), and was actively running all those socket connections in the background for each token each time (which can cause significant network slowdown), and since it is a game with constant update ticks this traffic grew very large. This should be accounted for as it caused solving to lose speed. This was all done on a singular device. JS API spoofs were only some basic navigator and window screen dimension edits. There was no order to solver solving. Each solver just attempted to solve as quickly as possible. With even more optimized config and setup for stealth, this could be further improved. Plus the potential for headless instance spawning with dockers exist, as is mentioned and will be mentioned throughout this repository. Initial solving rates were much faster, which it spawning 50 and even 100 in much less time. So, with optimized config and possible user-agent order calling, plus more solvers, this could absolutely be improved. I may run a test with even more solvers and see the result later.

---

# Proxy Formats

`protocol://host:port`
`protocol://user:pass@host:port`

The http protocol is recommended. Some browsers have iffy implementation for socks proxies.

---

## Components

The bypass is comprised of four main components:

1. **Token Harvester / Turnstile Widget Loader**
2. **Turnstile Widget Identifier & Clicker**
3. **Token Server**
4. **Proxy Extensions**

---

### 1. Token Harvester / Turnstile Widget Loader

The Token Harvester loads the Turnstile widget by spawning iframe-based solvers, each pointing at a different Cloudflare site widget. Every solver iframe connects to the token server and forwards any solved tokens to it, which is done once it receives an on demand request from your backend/receivers.

**Setup:**

1. **Configure the files.** There is config in `index.html`:
   - Set `TOKEN_SERVER_HOST` (your token server host, obviously), `PROXY_CONNECT_TIMEOUT` (time for proxy connection to timeout and page to begin reloading), and `USE_PROXY_SOLVING` (unless you just want to use a single IP to solve, keep this as true).
   - `PRELOAD_IFRAMES` is deprecated and may be used if future iframe tunneling implementation is added. For now keep at one.
   - Do not touch the declarations set to localStorage values. They were originally intended to be file config, but caching in localStorage is far cleaner.
  
2. **Set `localStorage.sitekey`**. to that website's Cloudflare sitekey.
   - If you do not know how to access a sitekey, here is a short and easy method you can use to access it: in devtools, find the turnstile.js file in the sources tab. In it, ctrl f "sitekey". You'll see many instances. You can breakpoint a few of these and then run the page to get into the scope, which will have the sitekey. 

3. **Set your proxies.**  Set your linesplit list of proxies to `localStorage.proxies`. The proxy extension will connect to a proxy from this list according to the received solver idx. Note the proxies list should include the protocol extension protocol://

4. **Apply as browser overrides.** Replace the target webpage's main HTML file with `index.html`. 

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

The Token Server doesn't participate in solving—it routes solver requests to available solvers, and forwards completed tokens back to their respective requesters. Solver iframes forward their tokens here as they're solved.

**Setup:**

Set the `PORT` value in config. That's all.

**Packet & Protocol Structure:**

*All values are little-endian.*

#### Serverbound (client -> server):

| Sent From | Header | Description |
|-----------|--------|-------------|
| Solver | `0` | Incoming token result from a solver. The server routes it back to the specific requester who asked for it by extracting the requester ID, then re-adds the solver to the available queue.<br><br>**Structure:** `<0, ...requester_id_bytes (u32), ...solver_idx_bytes (u32), ...token_bytes>`<br>*Note: If the solver failed to get a token, then there are no token bytes.* |
| Receiver | `1` | On-demand solve request from a requester. The server pulls the next available solver from the queue and forwards this assignment to them.<br><br>**User-Agent Routing:** You can specify a specific user-agent in this packet, which will then make the token server force a solver with that user-agent. This is particularly useful for mimicking real web traffic, and distributing solves across an amount that mimics the real web traffic distribution of user-agents. You can also just leave user-agent as `""` for a random selection.<br><br>**Field Spoofing:** The `fields` data allows you to implement JS field spoofs for a few things:<br>• **JS APIs:** You can spoof JS APIs like navigator properties and window dimensions by specifying `navigator.property`, `window.property`, etc. You can spoof with whatever JS properties you'd like basically. Window/viewport dimensions, navigator properties, etc. are all great properties you can spoof. However, so as to not confuse it with another field type (the next we will talk about), your JS field spoofs should refer to names in the structure of `API.key`. Nested references, like `API.key.key`, are also fine. For your field values, though obviously for the protocol they must be passed in as string data, if the values are directly castable to other primitive types (number, boolean), they will be automatically converted to such by the solvers for their logic. Otherwise, if not directly convertable to said types, they will be kept as strings.<br>• **Render Calls:** The `turnstile.render` function call, which initializes the widget, can take in special fields and extra data, such as `action`, or `cData`. To counter this, you may also specify field data for these in this packet. To specify field data for this, simply make the field name data you pass in the form of `key`. This contrasts from the `API.key` structure of the first case, and the system will know you are referring to a custom render call field. These fields will then be passed into the render call the solver makes.<br><br>**Structure:** `<1, ...solver_idx_bytes (u32), user_agent_len (u8), ...user_agent_bytes ...(field_name_len (u8), ...field_name_bytes, field_value_len (u8), ...field_value_bytes)>` |
| Solver | `2` | Register the sending socket as a solver. The server appends its socket ID to the available solvers queue.<br><br>**Queue Buckets:** It appends the socket ID to the solver queue bucket that matches the specified user-agent provided by the solver. If a bucket/HashSet for such does not exist yet, then it is created and the solver's socket ID is added to it. A user-agent can be referred to by the receiver when making requests, which will force only a solver with the matching user-agent to solve the request.<br><br>**Structure:** `<2, ...user_agent_bytes>` |
| Receiver | `3` | Request the total available solvers count. Good for analyzing how many active solving instances you can spawn.<br><br>**Structure:** `<3>` |

#### Clientbound (server -> client):

| Endpoint | Name | Description |
|----------|------|-------------|
| Receiver | Token | Incoming token delivered to a requester.<br><br>**Structure:** `<...solver_idx_bytes (u32), ...token_bytes>`<br>*Note: If the solver failed to get a token, then there are no token bytes.* |
| Receiver | Solvers Unavailable | A request made by a solver could not be completed because no solvers were available to accept it.<br><br>**Structure:** `<0>` |
| Solver | Solve Request | Solve a turnstile widget request that is delivered to a solver. Field data is parsed and does whatever is necessary (`API.key` -> JavaScript API is spoofed with the given field value, `key` -> turnstile render call adds this field).<br><br>**Structure:** `<...solver_idx_bytes (u32), ...requester_id_bytes (u32), ...(field_name_len (u8), ...field_name_bytes, field_value_len (u8), ...field_value_bytes)>` |
| Receiver | Available Solvers Result | The result to the available solvers count request you made.<br><br>**Length Collision Fix:** Note, the zero at the end of this packet is dummy data. It is actually added because I made the accepted parsing system for these packets length-based to check packet type, but the token packet will deliver 4 bytes if it fails to receive a token. I added the extra byte to this packet to solve the length collision because no branching logic is required for this one, and it's a much smaller and simpler case so I just preferred it.<br><br>**Structure:** `<...available_solvers_bytes (u32), 0>` |

> **Note:** The clientbound packets do not have headers since each endpoint receives few, easily discernible packets. Receivers receive a packet of only length 1 (Solvers Unavailable), the token packet itself (can be length 4 if there is no token and the request failed), or a packet of length 5 (total available solvers). This makes discerning packets by length easy. The solver can only receive a solve request.

**How it works:**

The architecture for the specific protocol of the server is above. The server assigns an ID to every socket, allows solvers to register themselves, for which it stores into available solver buckets (HashSets accessed by an outer HashMap that uses the respective user-agents as keys, meaning you can refer to solvers with specific user-agents only). Receivers can then simply send packets to the server to request solves from solvers, which if the solvers are available the server will forward. The solvers will send the solve results to the server, which will then forward it back to the original requester, which it does by bouncing around the original `requester_id` within these packets.

---

### 4. Proxy Extensions

The extensions allow us to utilize browser proxy API capabilities to connect to proxies, per tab. They also have JS API anti-fingerprint/spoof metrics, along with a WebRTC host peeking block.

For each browser you'll be using, you'll need to add the respective extension for that browser from `proxy-extensions` to whatever browser you are using (ex. firefox for firefox, cdp for cdp browsers, truly a shocker), and run it. These extensions provide the API necessary for asynchronous proxy connections, allowing you to await and connect to a proxy before continuing execution. 

## How It Works

Each extension acts as a bridge for proxy routing and fingerprint spoofing, driven by `window.postMessage` events. The steps to the execution flow are detailed below:

1. **Initialization**
   The extension listens for a `SET_TAB_PROXY` message sent by the client (which our solvers use). This payload contains the target proxy details and the specific JavaScript field data you want to spoof. *(Note: See the token server section for details on structuring this field data).*

2. **Proxy Routing**
   The extension applies the requested proxy. Because protocols vary by browser, this is handled in one of two ways: it either establishes a direct proxy connection for the tab, or it actively listens for outgoing requests and applies the proxy details to them on the fly.

3. **JS API Spoofing**
   The requested JavaScript APIs are spoofed by overriding native prototypes. This is done using a hidden Symbol key reference pointing to a modifiable entry. This architecture is critical: it allows our script to dynamically overwrite fields with new values without breaking the page, while completely hiding the spoofing metrics from anti-bot systems like Cloudflare.

4. **WebRTC Leak Prevention**
   To maintain operational security, WebRTC host peeking and STUN search features are disabled. This strictly blocks WebRTC host IP leaks while keeping standard WebRTC functionality enabled.

5. **matchMedia Protection**
   `matchMedia`, a method that runs on the CSS engine, can get your real window dimensions if you spoof standard JS window dimension values. It can check and compare values like `width`/`min-width`/`max-width`, or `height`/`min-height`/`max-height`. `matchMedia` returns data in a `matches` property of the response structure, which is a boolean determining if the given query matches or aligns with the actual session's data. 

   If you provide `window.innerWidth` and `window.innerHeight` fields (though to fully spoof well, you'll need to spoof other fields—these just trigger the feature, as mediaQuery compares all pixel data in relation to those dimensions), `matchMedia` will be spoofed. For width and height checks, it will force the `matches` field to output the exact result it would give if your window were actually the dimensions of your spoofed `window.innerWidth` and `window.innerHeight`.

7. **Execution Readiness**
   Once the proxy is fully connected and the environment is secured, the extension sends a `PROXY_READY` message back to the client. This allows solvers to securely await a confirmed proxy connection before continuing their execution.

---

## Starting It Up

1. Start the **token server**.
2. Start the **auto-clicker**.
3. Open your **modified webpages**.
4. Press **F8** to enable the auto-clicker.
5. Start your backend, token managing and requesting system. 
6. Watch it go.

---

## Some Helpers for your Backend

Your backend that actually gets and requests solves for tokens will need to interact with the token server. 

You will need a reference to a proxies txt list. This list should match the one you set at localStorage.proxies on the solver page.

For any turnstile render call custom fields, such as "cData" or "action" as previously mentioned, you'll need to figure out how they are generated for your target, and recreate the logic to how these fields are generated so that you can pass them into your solve request packet. "action" is usually a hardcoded string, but "cData" is often used as an individual ID/verification field. In short, ensure all fields of the turnstile render call match.

For any JavaScript API fields you'd like to spoof, you'll also need to send that data into the fields arguments of the construct_solver_request_packet. Details on how to structure the fields data is provided in previous sections (see token server section).

**Construct solve request packet:**

```javascript
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

```javascript
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

```javascript
// packet = packet buffer.
function parse_available_solvers_count_packet(packet) {
    let view = new DataView(packet);
    return [view.getUint32(0, true)];
};
```

**Match packets:**

```javascript
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

Support (extensions) for more browsers.

Automatic, headless page loading with a docker. Plus Devtools protocol level overriding so standard overrides aren't required. Dockerizing and making this project headless could be huge for maximizing throughput. Plus it'd just make it feel less like a PoC and more like a fully fleshed out project.

Canvas fingerprint spoofing to match given hardware specs.

If a feasible solution is found, a way to tunnel individual iframes (hence enhancing multi-proxy solving outside of just different tabs) may be implemented.

---

## Contributing

All contributions are very welcome. If you have a way to improve this project, please share with issues, pull requests, etc.

---
