# 网关 CA 证书

`dgw-ca.crt` 是校验网关服务端证书链的根，**不是服务端证书本身**。
文件里只有一张自签证书，没有私钥（1436 字节，一张 `BEGIN CERTIFICATE`），
发布它不会泄露任何凭据。

| 项 | 值 |
|---|---|
| 来源 | 厂商 `tgw` 1.0.9.2 wheel 里的 `tgw/cert/.ca.crt`，原样拷贝 |
| subject / issuer | `C=CN, ST=GuangDong, L=Shenzhen, O=archforce, OU=AMD, CN=*.dgw.com` |
| 有效期 | 2021-07-21 ~ **2031-04-20** |
| SHA1 | `45:1F:1C:ED:21:FD:17:5E:A8:48:FD:60:2F:C2:D3:BA:0E:21:DE:94` |

## 为什么要它

网关按**域名**签发服务端证书（CN 落在 `*.dgw.com`），不走公共 CA。
直接用 IP 连会报 `IP address mismatch`——校验得靠
`server_hostname='www.dgw.com'` 做 SNI（见 `tgwpy/client.py` 的
`TLS_SERVER_NAME`），而验链要的就是这张根证书。

## 什么时候要换

- **2031-04-20 之后**：这张根证书过期，握手必然失败。
- **服务端换签发者**：握手报 `certificate verify failed`，而对端 IP 没变。

两种情况都指向同一个动作——把新证书放到本地任意位置，用环境变量指过去：

```bash
export TGW_CA_CERT=/path/to/new-ca.crt
```

`TGW_CA_CERT` 优先于本目录这份（解析在 `tgwpy.client.resolve_ca_cert`，
`client.py` 与 `probe.py` 共用）。指向的文件不存在时报错会带上那个路径，
不会退回去用仓内这份——**用错根证书时静默换一张，比直接失败更难查**。
