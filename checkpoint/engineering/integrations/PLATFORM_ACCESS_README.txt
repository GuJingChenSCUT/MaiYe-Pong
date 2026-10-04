平台接入準備增補（2026-10-04）

原 adapters.py 的所有交易功能仍禁用，DESIGN.txt 的交易/幣種/UNKNOWN 邊界不变。
新 taobao_top.py、hktvmall_mms.py 是離線請求/簽名準備器，沒有transport、OAuth、credential loader或業務route。
新 browser_research.py 是離線策略，不是browser executor，不能確認付款或商戶訂單。
TOP只包含兩個授權賣家讀取方法；HKTVmall只包含四個商戶管理GET，兩個保留JSON body。
HKTVmall正式環境only，官方目前沒有獨立sandbox。空grant預設拒絕。
詳見包內verification/platform_access_20261004/guide.html、研究JSON與實際離線測試日誌。
準備器的敏感對象repr已遮蔽，但显式屬性、asdict、wire_headers仍會揭露值，不能輸出給模型或前端。
