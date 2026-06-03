from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel
from typing import Literal
import subprocess

from generate_whitelist import generate_iptables_whitelist
from generate_open_rules import generate_iptables_open_rules

app = FastAPI(title="iptables 策略管理服务", version="1.0.0")


class WhitelistPolicy(BaseModel):
    """白名单策略"""
    policy_type: Literal["whitelist"] = "whitelist"
    allowed_ips: list[str]

class OpenPolicy(BaseModel):
    """开放策略"""
    policy_type: Literal["open"] = "open"


Policy = WhitelistPolicy | OpenPolicy



def apply_iptables_rules(rules_content: str) -> bool:
    """应用 iptables 规则"""
    import os
    target_dir = "/etc/iptables"
    target_path = f"{target_dir}/rules.v4"

    try:
        # 创建目录
        os.makedirs(target_dir, exist_ok=True)

        # 写入规则文件
        with open(target_path, 'w') as f:
            f.write(rules_content)
        print(f"✅ 规则已写入: {target_path}")

        # 执行 iptables-restore
        subprocess.run(
            ["iptables-restore", "<", target_path],
            shell=True,
            check=True
        )
        print(f"✅ iptables 规则已生效")
        return True

    except subprocess.CalledProcessError as e:
        print(f"❌ 失败: {e}")
        return False
    except Exception as e:
        print(f"❌ 错误: {e}")
        return False


@app.post("/egress/policy")
def apply_policy(policy: Policy, request: Request):
    """
    下发 iptables 策略

    **白名单模式**：
    ```json
    {
        "policy_type": "whitelist",
        "allowed_ips": ["192.168.1.100", "10.0.0.5"]
    }
    ```

    **开放模式**：
    ```json
    {
        "policy_type": "open"
    }
    ```
    """
    # 安全检查：验证管理端 IP
    client_host = request.client.host



    if policy.policy_type == "whitelist":
        allowed_ips = policy.allowed_ips.copy()

        # 确保管理端 IP 在白名单中（防止把自己锁在外面）
        if client_host not in allowed_ips:
            allowed_ips.append(client_host)
            print(f"⚠️  已自动添加管理端 IP 到白名单: {client_host}")

        print(f"🚀 下发白名单策略")
        print(f"   允许的 IP: {allowed_ips}")

        rules = generate_iptables_whitelist(allowed_ips)
        success = apply_iptables_rules(rules)

        if success:
            return {
                "status": "success",
                "message": "白名单策略已应用",
                "policy_type": "whitelist",
                "allowed_ips": allowed_ips
            }
        else:
            raise HTTPException(
                status_code=500,
                detail="策略应用失败"
            )

    elif policy.policy_type == "open":
        print(f"🚀 下发开放策略")

        rules = generate_iptables_open_rules()
        success = apply_iptables_rules(rules)

        if success:
            return {
                "status": "success",
                "message": "开放策略已应用",
                "policy_type": "open"
            }
        else:
            raise HTTPException(
                status_code=500,
                detail="策略应用失败"
            )


@app.get("/egress/policy")
def get_rules():
    """查看当前 iptables 规则"""
    try:
        result = subprocess.run(
            ["iptables", "-L", "-n"],
            capture_output=True,
            text=True,
            check=True
        )
        return {
            "rules": result.stdout
        }
    except subprocess.CalledProcessError:
        raise HTTPException(
            status_code=500,
            detail="无法获取 iptables 规则"
        )


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
