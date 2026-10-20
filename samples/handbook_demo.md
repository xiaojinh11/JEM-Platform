# 华为 VRP 常用命令速查（演示数据）

## 接口配置

```bash
interface GigabitEthernet0/0/1
 description uplink-to-core
 port link-type trunk
 port trunk allow-pass vlan 10 20 30
```

## VLAN 创建

```bash
vlan 100
 description SERVER-DB
```

## OSPF

```bash
ospf 1 router-id 1.1.1.1
 area 0.0.0.0
  network 10.0.0.0 0.255.255.255
```

## ACL

```bash
acl number 3000
 rule 5 deny ip source 192.168.1.0 0.0.0.255 destination 10.10.10.0 0.0.0.255
 rule 10 permit ip
```

## 常用排错命令

- `display version`
- `display current-configuration`
- `display interface brief`
- `display cpu-usage`
- `display logbuffer`
- `display arp`
- `display mac-address`
