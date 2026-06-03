def generate_iptables_open_rules() -> str:
    """
    生成 iptables 允许所有流量的规则文件内容

    返回:
        iptables 规则文件内容，可直接写入 /etc/iptables/rules.v4
    """
    rules = []

    # iptables-restore 格式头部
    rules.append("*filter")
    rules.append("")

    # 设置默认策略为 ACCEPT（允许所有）
    rules.append("# 默认策略：允许所有流量")
    rules.append(":INPUT ACCEPT [0:0]")
    rules.append(":FORWARD ACCEPT [0:0]")
    rules.append(":OUTPUT ACCEPT [0:0]")
    rules.append("")

    # 提交规则
    rules.append("COMMIT")

    return "\n".join(rules)

