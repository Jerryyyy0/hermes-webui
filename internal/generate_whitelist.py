def generate_iptables_whitelist(allowed_ips: list[str]) -> str:
    """
    生成 iptables 白名单规则文件内容

    参数:
        allowed_ips: 允许的 IP 地址列表，例如 ['192.168.1.100', '10.0.0.5']

    返回:
        iptables 规则文件内容，可直接写入 /etc/iptables/rules.v4
    """
    rules = []

    # iptables-restore 格式头部
    rules.append("*filter")
    rules.append("")

    # 设置默认策略为 DROP（拒绝所有）
    rules.append("# 默认策略：拒绝所有流量")
    rules.append(":INPUT DROP [0:0]")
    rules.append(":FORWARD DROP [0:0]")
    rules.append(":OUTPUT DROP [0:0]")
    rules.append("")

    # 允许本地回环接口（localhost）
    rules.append("# 允许本地回环接口")
    rules.append("-A INPUT -i lo -j ACCEPT")
    rules.append("-A OUTPUT -o lo -j ACCEPT")
    rules.append("")

    # 允许已建立和相关的连接
    rules.append("# 允许已建立和相关连接的响应流量")
    rules.append("-A INPUT -m state --state ESTABLISHED,RELATED -j ACCEPT")
    rules.append("-A OUTPUT -m state --state ESTABLISHED,RELATED -j ACCEPT")
    rules.append("")

    # 添加白名单 IP 规则
    rules.append("# 白名单：允许指定的 IP 地址")
    for ip in allowed_ips:
        if ip.strip():  # 跳过空行
            rules.append(f"# 允许 IP: {ip}")
            # INPUT 链：允许来自该 IP 的入站流量
            rules.append(f"-A INPUT -s {ip} -j ACCEPT")
            # OUTPUT 链：允许发往该 IP 的出站流量
            rules.append(f"-A OUTPUT -d {ip} -j ACCEPT")
            rules.append("")

    # 可选：允许 DNS（如果需要）
    rules.append("# 可选：允许 DNS 查询（如需要）")
    rules.append("#-A OUTPUT -p udp --dport 53 -j ACCEPT")
    rules.append("#-A OUTPUT -p tcp --dport 53 -j ACCEPT")
    rules.append("")

    # 提交规则
    rules.append("COMMIT")

    return "\n".join(rules)


