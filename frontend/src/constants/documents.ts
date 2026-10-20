/**
 * Document management constants.
 * Vendor list curated for common network/security device vendors.
 * All vendor names normalized to short lowercase keys (no spaces, no slash).
 */

export const VENDORS: string[] = [
  // ── 主流网络设备厂商 ──
  '华为',
  '新华三',
  '思科',
  '锐捷',
  '中兴',
  '迈普',
  '博达',
  '神州数码',
  'Juniper',
  'Aruba',
  'TP-Link',
  'D-Link',
  'NetGear',
  'HPE-Aruba',
  // ── 防火墙 / 安全 ──
  '天融信',
  '深信服',
  '山石网科',
  'Fortinet',
  '迪普科技',
  '启明星辰',
  '绿盟科技',
  '奇安信',
  '安恒信息',
  '亚信安全',
  '360',
  'NSFOCUS',
  'Topsec',
  'CheckPoint',
  'PaloAlto',
  // ── 其他 / 自定义 ──
  '其他',
]

/**
 * Vendor quick-links for the global sidebar (AppLayout).
 * Each entry: { name, url, group } — `group` is purely visual, used to
 * fold the list into "网络 / 安全" sections in the side panel.
 */
export interface VendorLink {
  name: string
  url: string
  group: '网络' | '安全'
}

export const VENDOR_LINKS: VendorLink[] = [
  // ── 网络 ──
  { name: '华为',      url: 'https://www.huawei.com/cn/',                 group: '网络' },
  { name: '新华三',    url: 'https://www.h3c.com/cn/',                    group: '网络' },
  { name: '思科',      url: 'https://www.cisco.com/c/zh_cn/index.html',   group: '网络' },
  { name: '锐捷',      url: 'https://www.ruijie.com.cn/',                 group: '网络' },
  { name: '中兴',      url: 'https://www.zte.com.cn/china/',              group: '网络' },
  { name: '迈普',      url: 'https://www.maipu.cn/',                      group: '网络' },
  { name: '博达',      url: 'https://www.bdcom.cn/',                      group: '网络' },
  { name: '神州数码',  url: 'https://www.digitalchina.com/',              group: '网络' },
  { name: 'Juniper',   url: 'https://www.juniper.net/',                   group: '网络' },
  { name: 'Aruba',     url: 'https://www.arubanetworks.com/',             group: '网络' },
  { name: 'TP-Link',   url: 'https://www.tp-link.com.cn/',                group: '网络' },
  { name: 'D-Link',    url: 'https://www.dlink.com.cn/',                  group: '网络' },
  { name: 'NetGear',   url: 'https://www.netgear.com.cn/',                group: '网络' },
  { name: 'HPE-Aruba', url: 'https://www.hpe.com/cn/zh.html',             group: '网络' },
  // ── 安全 ──
  { name: '天融信',    url: 'https://www.topsec.com.cn/',                 group: '安全' },
  { name: '深信服',    url: 'https://www.sangfor.com.cn/',                group: '安全' },
  { name: '山石网科',  url: 'https://www.hillstonenet.com.cn/',           group: '安全' },
  { name: 'Fortinet',  url: 'https://www.fortinet.com/cn/',               group: '安全' },
  { name: '迪普科技',  url: 'https://www.dptech.com/',                    group: '安全' },
  { name: '启明星辰',  url: 'https://www.venustech.com.cn/',              group: '安全' },
  { name: '绿盟科技',  url: 'https://www.nsfocus.com.cn/',                group: '安全' },
  { name: '奇安信',    url: 'https://www.qianxin.com/',                   group: '安全' },
  { name: '安恒信息',  url: 'https://www.dbappsecurity.com.cn/',          group: '安全' },
  { name: '亚信安全',  url: 'https://www.asiainfo-sec.com/',              group: '安全' },
  { name: '360',       url: 'https://360.cn/',                            group: '安全' },
  { name: 'NSFOCUS',   url: 'https://www.nsfocus.com/',                   group: '安全' },
  { name: 'Topsec',    url: 'https://www.topsec.com/',                    group: '安全' },
  { name: 'CheckPoint', url: 'https://www.checkpoint.com/',               group: '安全' },
  { name: 'PaloAlto',  url: 'https://www.paloaltonetworks.cn/',           group: '安全' },
]

/**
 * Document upload: only a single fixed category going forward.
 * Kept as a single-item list so the UI shows a stable label.
 */
export const CATEGORIES: string[] = ['产品手册']

/**
 * File extensions accepted by the uploader UI hint + validator.
 * Intentionally permissive: whoosh indexer only reads text;
 * binary formats are stored as-is and surfaced as opaque blobs.
 */
export const ACCEPTED_EXTENSIONS: string[] = [
  // ── 文本 ──
  '.md', '.txt', '.log', '.csv',
  // ── Office ──
  '.doc', '.docx', '.xls', '.xlsx', '.ppt', '.pptx',
  // ── PDF / 图书 ──
  '.pdf', '.html', '.htm', '.mhtml', '.mdi', '.xps', '.epub',
  // ── 华为专属 ──
  '.hwics', '.hdx',
  // ── 思科 / 设备 ──
  '.bin', '.tar', '.pkg',
  // ── 压缩 ──
  '.zip', '.7z', '.rar', '.gz', '.tgz', '.tar.gz',
  // ── 配置 / 脚本 ──
  '.cfg', '.dat', '.ini', '.conf', '.yaml', '.yml', '.xml', '.json', '.py', '.sh',
  // ── 其他 ──
  '.chm', '.mib',
]

export const ACCEPT_ATTR = ACCEPTED_EXTENSIONS.join(',')
