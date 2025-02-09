import json
import os
from typing import Dict

# 在这里添加IP地址映射
# 格式: "IP地址": "备注名"
DEFAULT_IP_MAPPINGS = {
    # 示例映射
    # "127.0.0.1": "本地测试",
    # "192.168.1.100": "办公室电脑",
    
    # 在下方添加你的IP映射
    "": "",  # 映射1
    "": "",  # 映射2
    "": "",  # 映射3
    "": "",  # 映射4
    "": "",  # 映射5
}

class IPMapper:
    def __init__(self, mapping_file: str):
        """
        初始化IP映射器
        :param mapping_file: IP映射文件的完整路径（由UserLogger提供）
        """
        self.mapping_file = mapping_file
        self.ip_mapping: Dict[str, str] = {}
        # 首先加载默认映射
        self.ip_mapping.update({k: v for k, v in DEFAULT_IP_MAPPINGS.items() if k and v})
        # 然后尝试从文件加载额外映射
        self._load_mapping()

    def _load_mapping(self):
        """从文件加载IP映射"""
        try:
            if os.path.exists(self.mapping_file):
                with open(self.mapping_file, 'r', encoding='utf-8') as f:
                    file_mappings = json.load(f)
                    # 合并文件中的映射，但不覆盖默认映射
                    for k, v in file_mappings.items():
                        if k not in DEFAULT_IP_MAPPINGS:
                            self.ip_mapping[k] = v
        except Exception as e:
            print(f"加载IP映射文件失败: {str(e)}")

    def _save_mapping(self):
        """保存IP映射到文件"""
        try:
            with open(self.mapping_file, 'w', encoding='utf-8') as f:
                json.dump(self.ip_mapping, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"保存IP映射文件失败: {str(e)}")

    def add_mapping(self, ip: str, remark: str):
        """添加或更新IP映射"""
        self.ip_mapping[ip] = remark
        self._save_mapping()

    def remove_mapping(self, ip: str):
        """删除IP映射"""
        if ip in self.ip_mapping:
            del self.ip_mapping[ip]
            self._save_mapping()

    def get_remark(self, ip: str) -> str:
        """获取IP对应的备注名，如果没有映射则返回原IP"""
        return self.ip_mapping.get(ip, ip)

    def list_mappings(self) -> Dict[str, str]:
        """列出所有IP映射"""
        return self.ip_mapping.copy() 