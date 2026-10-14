"""Generate SYNTHETIC Trading API GetOrders responses for tests (no real data)."""

BUYER = """
      <BuyerUserID>PII_SENTINEL_userid</BuyerUserID>
      <ShippingAddress>
        <Name>PII_SENTINEL_name</Name>
        <Street1>PII_SENTINEL_street</Street1>
        <CityName>PII_SENTINEL_city</CityName>
        <PostalCode>PII_SENTINEL_pc</PostalCode>
        <Country>GB</Country>
        <Phone>PII_SENTINEL_phone</Phone>
      </ShippingAddress>"""


def txn(item, title, price, txn_id, srn, qty=1, created="2026-10-10T09:00:00.000Z", actual_ship=None):
    ship = f"<ActualShippingCost currencyID=\"GBP\">{actual_ship}</ActualShippingCost>" if actual_ship else ""
    return f"""
        <Transaction>
          <Buyer><Email>pii.sentinel@example.invalid</Email><UserFirstName>PII_SENTINEL_first</UserFirstName><UserLastName>PII_SENTINEL_last</UserLastName></Buyer>
          <ShippingDetails><SellingManagerSalesRecordNumber>{srn}</SellingManagerSalesRecordNumber></ShippingDetails>
          <CreatedDate>{created}</CreatedDate>
          <Item><ItemID>{item}</ItemID><Title>{title}</Title></Item>
          <QuantityPurchased>{qty}</QuantityPurchased>
          <TransactionID>{txn_id}</TransactionID>
          <TransactionPrice currencyID="GBP">{price}</TransactionPrice>{ship}
        </Transaction>"""


def order(oid, txns, shipping="0.00", status="Completed", paid=True, cancel="NotApplicable",
          payment="NoPaymentFailure", created="2026-10-10T09:00:00.000Z"):
    paid_el = "<PaidTime>2026-10-10T09:05:00.000Z</PaidTime>" if paid else ""
    return f"""
    <Order>
      <OrderID>{oid}</OrderID>
      <CheckoutStatus><eBayPaymentStatus>{payment}</eBayPaymentStatus><Status>Complete</Status></CheckoutStatus>
      <OrderStatus>{status}</OrderStatus>
      <CancelStatus>{cancel}</CancelStatus>
      <CreatedTime>{created}</CreatedTime>
      {paid_el}{BUYER}
      <ShippingServiceSelected><ShippingService>UK_RoyalMailSecondClassStandard</ShippingService>
        <ShippingServiceCost currencyID="GBP">{shipping}</ShippingServiceCost></ShippingServiceSelected>
      <Total currencyID="GBP">0.00</Total>
      <TransactionArray>{''.join(txns)}
      </TransactionArray>
    </Order>"""


def page(orders, has_more, page_no, total_pages):
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<GetOrdersResponse xmlns="urn:ebay:apis:eBLBaseComponents">
  <Timestamp>2026-10-14T12:00:00.000Z</Timestamp>
  <Ack>Success</Ack>
  <Version>1199</Version>
  <PaginationResult><TotalNumberOfPages>{total_pages}</TotalNumberOfPages><TotalNumberOfEntries>5</TotalNumberOfEntries></PaginationResult>
  <HasMoreOrders>{'true' if has_more else 'false'}</HasMoreOrders>
  <OrderArray>{''.join(orders)}
  </OrderArray>
  <PageNumber>{page_no}</PageNumber>
</GetOrdersResponse>"""


p1 = page([
    order("31-00001-00001", [txn("400000000001", "Volkswagen Golf Mk7 WINDOW MOTOR (FRONT DRIVER) 5Q0 959 802 A",
                                 "29.99", "9000000001", "8001")]),
    # multi-line order, £6.99 order-level postage shared across 2 lines
    order("31-00002-00002", [txn("400000000002", "Seat Ibiza 6J WIPER MOTOR (FRONT) 6J2 955 119", "40.00",
                                 "9000000002", "8002", qty=2),
                             txn("400000000003", "Seat Ibiza 6J BONNET RELEASE CABLE 6J1 823 535", "10.00",
                                 "9000000003", "8003")], shipping="6.99"),
], has_more=True, page_no=1, total_pages=2)

p2 = page([
    order("31-00003-00003", [txn("400000000004", "Skoda Fabia 5J HEADLIGHT (DRIVER) 5J1 941 016", "45.00",
                                 "9000000004", "8004")], cancel="CancelComplete"),           # cancelled
    order("31-00004-00004", [txn("400000000005", "VW Polo 6R SPEEDO CLOCKS 6R0 920 860 A", "35.00",
                                 "9000000005", "8005")], paid=False, status="Active"),       # unpaid
    order("31-00005-00005", [txn("400000000006", "Audi A3 8V ENGINE ECU 04L 907 309 R", "89.99",
                                 "9000000006", "8006", actual_ship="4.50")]),
], has_more=False, page_no=2, total_pages=2)

auth = """<?xml version="1.0" encoding="UTF-8"?>
<GetOrdersResponse xmlns="urn:ebay:apis:eBLBaseComponents">
  <Ack>Failure</Ack>
  <Errors><ShortMessage>Auth token is invalid.</ShortMessage><ErrorCode>931</ErrorCode><SeverityCode>Error</SeverityCode></Errors>
</GetOrdersResponse>"""

empty = page([], has_more=False, page_no=1, total_pages=0)

for name, body in (("getorders_page1.xml", p1), ("getorders_page2.xml", p2),
                   ("getorders_auth_error.xml", auth), ("getorders_empty.xml", empty)):
    open(f"tests/fixtures/{name}", "w", encoding="utf-8", newline="\n").write(body)
print("fixtures written")
