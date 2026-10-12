import React, { useState, useEffect } from 'react';

export default function App() {
  const [tab, setTab] = useState('catalogue');
  const [dealers, setDealers] = useState([]);
  const [dealerId, setDealerId] = useState(1);
  const [products, setProducts] = useState([]);
  const [cart, setCart] = useState([]);
  const [orders, setOrders] = useState([]);
  const [search, setSearch] = useState('');
  const [category, setCategory] = useState('');
  const [chatMessages, setChatMessages] = useState([
    { role: 'model', content: 'Hello! I am your VIKMO AI Dealer Assistant. Ask about stock, catalogue parts, or creating draft orders.' }
  ]);
  const [chatInput, setChatInput] = useState('');
  const [sessionId] = useState(() => 'sess-' + Math.random().toString(36).substring(2, 9));
  const [reorders, setReorders] = useState([]);
  const [insufficientStockError, setInsufficientStockError] = useState(null);

  useEffect(() => {
    fetch('/api/dealers/?page_size=25')
      .then(res => res.json())
      .then(d => {
        if (d.results) {
          setDealers(d.results);
          if (d.results.length > 0) setDealerId(d.results[0].id);
        }
      });
  }, []);

  useEffect(() => {
    let url = `/api/products/?page_size=40`;
    if (search) url += `&search=${encodeURIComponent(search)}`;
    if (category) url += `&category=${encodeURIComponent(category)}`;
    fetch(url).then(res => res.json()).then(d => setProducts(d.results || []));
  }, [search, category]);

  const loadOrders = () => {
    fetch(`/api/orders/?dealer_id=${dealerId}`)
      .then(res => res.json())
      .then(d => setOrders(d.results || []));
  };

  const loadReorders = () => {
    fetch('/api/reorder-suggestions/')
      .then(res => res.json())
      .then(d => setReorders(d.results || []));
  };

  useEffect(() => {
    if (tab === 'orders') loadOrders();
    if (tab === 'reorder') loadReorders();
  }, [tab, dealerId]);

  const addToCart = (product, qty = 1) => {
    setCart(prev => {
      const existing = prev.find(x => x.sku === product.sku);
      if (existing) {
        return prev.map(x => x.sku === product.sku ? { ...x, quantity: x.quantity + qty } : x);
      }
      return [...prev, { sku: product.sku, name: product.name, price: parseFloat(product.price), quantity: qty }];
    });
  };

  const createDraftOrder = async () => {
    if (cart.length === 0) return alert('Cart is empty');
    const res = await fetch('/api/orders/', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        dealer_id: dealerId,
        items: cart.map(i => ({ sku: i.sku, quantity: i.quantity })),
      }),
    });
    const data = await res.json();
    if (res.status === 201) {
      alert(`Draft Order ${data.order_number} created successfully!`);
      setCart([]);
      loadOrders();
    } else {
      alert(data.error?.message || 'Order creation failed');
    }
  };

  const confirmOrder = async (orderNumber) => {
    setInsufficientStockError(null);
    const res = await fetch(`/api/orders/${orderNumber}/confirm/`, { method: 'POST' });
    const data = await res.json();
    if (res.status === 200) {
      alert(`Order ${orderNumber} confirmed! Stock deducted.`);
      loadOrders();
    } else if (res.status === 409 && data.error?.code === 'INSUFFICIENT_STOCK') {
      setInsufficientStockError(data.error);
    } else {
      alert(data.error?.message || 'Confirmation error');
    }
  };

  const sendChat = async (msg) => {
    const text = msg || chatInput;
    if (!text.trim()) return;
    setChatInput('');
    setChatMessages(prev => [...prev, { role: 'user', content: text }]);
    const res = await fetch('/api/assistant/chat/', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ session_id: sessionId, dealer_id: dealerId, message: text }),
    });
    const data = await res.json();
    setChatMessages(prev => [...prev, {
      role: 'model',
      content: data.reply,
      toolCalls: data.tool_calls,
      citedSkus: data.cited_skus,
      actions: data.actions,
    }]);
  };

  return (
    <div style={{ minHeight: '100vh', background: '#080B11', color: '#F3F4F6', fontFamily: 'Plus Jakarta Sans, sans-serif' }}>
      <header style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', padding: '1rem 2rem', background: '#0E131F', borderBottom: '1px solid #1E283D' }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: '0.75rem', fontWeight: 800, fontSize: '1.25rem' }}>
          <span style={{ color: '#00F0FF' }}>VIKMO</span> DealerDesk
        </div>
        <div style={{ display: 'flex', gap: '0.5rem' }}>
          {['catalogue', 'orders', 'assistant', 'reorder'].map(t => (
            <button key={t} onClick={() => setTab(t)} style={{
              background: tab === t ? '#141A29' : 'transparent',
              color: tab === t ? '#00F0FF' : '#9CA3AF',
              border: tab === t ? '1px solid #00F0FF' : 'none',
              padding: '0.5rem 1rem', borderRadius: 8, cursor: 'pointer', fontWeight: 600, textTransform: 'capitalize'
            }}>
              {t}
            </button>
          ))}
        </div>
        <div style={{ display: 'flex', alignItems: 'center', gap: '1rem' }}>
          <select value={dealerId} onChange={e => setDealerId(parseInt(e.target.value))} style={{ background: '#141A29', color: '#F3F4F6', padding: '0.4rem', borderRadius: 6, border: '1px solid #1E283D' }}>
            {dealers.map(d => <option key={d.id} value={d.id}>{d.dealer_code} - {d.name}</option>)}
          </select>
          <div style={{ background: 'rgba(0,240,255,0.1)', border: '1px solid rgba(0,240,255,0.3)', padding: '0.4rem 0.8rem', borderRadius: 8 }}>
            Cart: {cart.reduce((a, b) => a + b.quantity, 0)} items
          </div>
        </div>
      </header>

      <main style={{ maxWidth: 1400, margin: '2rem auto', padding: '0 2rem' }}>
        {tab === 'catalogue' && (
          <div>
            <div style={{ display: 'flex', gap: '1rem', marginBottom: '1.5rem' }}>
              <input value={search} onChange={e => setSearch(e.target.value)} placeholder="Search parts..." style={{ flex: 1, padding: '0.75rem', background: '#141A29', border: '1px solid #1E283D', borderRadius: 8, color: '#fff' }} />
              <select value={category} onChange={e => setCategory(e.target.value)} style={{ padding: '0.75rem', background: '#141A29', border: '1px solid #1E283D', borderRadius: 8, color: '#fff' }}>
                <option value="">All Categories</option>
                <option value="Braking">Braking</option>
                <option value="Filters">Filters</option>
                <option value="Lighting & Electrical">Lighting & Electrical</option>
                <option value="Drivetrain">Drivetrain</option>
              </select>
            </div>
            <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(280px, 1fr))', gap: '1rem' }}>
              {products.map(p => (
                <div key={p.sku} style={{ background: '#141A29', border: '1px solid #1E283D', borderRadius: 12, padding: '1rem', display: 'flex', flexDirection: 'column', justifyContent: 'space-between' }}>
                  <div>
                    <div style={{ display: 'flex', justifyContent: 'space-between' }}>
                      <span style={{ color: '#00F0FF', fontWeight: 700, fontSize: '0.8rem' }}>{p.sku}</span>
                      <span style={{ color: p.stock > 0 ? '#10B981' : '#EF4444', fontSize: '0.8rem', fontWeight: 700 }}>{p.stock} in stock</span>
                    </div>
                    <h3 style={{ margin: '0.5rem 0', fontSize: '1rem' }}>{p.name}</h3>
                    <div style={{ fontSize: '0.8rem', color: '#9CA3AF' }}>{p.brand} • {p.vehicle_fitment}</div>
                  </div>
                  <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginTop: '1rem' }}>
                    <span style={{ fontWeight: 800, fontSize: '1.1rem' }}>₹{p.price}</span>
                    <button onClick={() => addToCart(p, 1)} style={{ background: '#00F0FF', color: '#080B11', border: 'none', padding: '0.4rem 0.8rem', borderRadius: 6, fontWeight: 700, cursor: 'pointer' }}>+ Cart</button>
                  </div>
                </div>
              ))}
            </div>
          </div>
        )}

        {tab === 'orders' && (
          <div>
            {insufficientStockError && (
              <div style={{ background: 'rgba(239,68,68,0.1)', border: '1px solid #EF4444', padding: '1rem', borderRadius: 8, marginBottom: '1rem' }}>
                <strong style={{ color: '#EF4444' }}>⚠️ Insufficient Stock Error</strong>
                <div>{insufficientStockError.message}</div>
                <ul>
                  {insufficientStockError.details?.map(d => (
                    <li key={d.sku}>{d.sku}: Requested={d.requested}, Available={d.available}</li>
                  ))}
                </ul>
              </div>
            )}
            <div style={{ display: 'grid', gridTemplateColumns: '1fr 340px', gap: '2rem' }}>
              <div>
                <h2>Cart Items</h2>
                {cart.length === 0 ? <p style={{ color: '#6B7280' }}>Empty cart</p> : (
                  <table style={{ width: '100%', borderCollapse: 'collapse', textAlign: 'left', marginBottom: '2rem' }}>
                    <thead>
                      <tr style={{ borderBottom: '1px solid #1E283D' }}><th>SKU</th><th>Name</th><th>Qty</th><th>Price</th><th>Subtotal</th></tr>
                    </thead>
                    <tbody>
                      {cart.map(item => (
                        <tr key={item.sku} style={{ borderBottom: '1px solid #1E283D' }}>
                          <td>{item.sku}</td><td>{item.name}</td><td>{item.quantity}</td><td>₹{item.price.toFixed(2)}</td><td>₹{(item.price * item.quantity).toFixed(2)}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                )}
                <h2>Orders</h2>
                <table style={{ width: '100%', borderCollapse: 'collapse', textAlign: 'left' }}>
                  <thead>
                    <tr style={{ borderBottom: '1px solid #1E283D' }}><th>Order Number</th><th>Status</th><th>Subtotal</th><th>GST (18%)</th><th>Total</th><th>Actions</th></tr>
                  </thead>
                  <tbody>
                    {orders.map(o => (
                      <tr key={o.order_number} style={{ borderBottom: '1px solid #1E283D' }}>
                        <td>{o.order_number}</td>
                        <td>{o.status}</td>
                        <td>₹{o.subtotal}</td>
                        <td>₹{o.tax_total}</td>
                        <td>₹{o.grand_total}</td>
                        <td>
                          {o.status === 'DRAFT' && <button onClick={() => confirmOrder(o.order_number)} style={{ background: '#00F0FF', border: 'none', padding: '0.3rem 0.6rem', borderRadius: 4, cursor: 'pointer' }}>Confirm</button>}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              <div style={{ background: '#141A29', padding: '1.5rem', borderRadius: 12, height: 'fit-content' }}>
                <h3>Summary</h3>
                <p>Items: {cart.length}</p>
                <p>Subtotal: ₹{cart.reduce((s, i) => s + i.price * i.quantity, 0).toFixed(2)}</p>
                <button onClick={createDraftOrder} style={{ width: '100%', padding: '0.75rem', background: '#00F0FF', color: '#080B11', border: 'none', borderRadius: 8, fontWeight: 700, cursor: 'pointer' }}>Create Draft Order</button>
              </div>
            </div>
          </div>
        )}

        {tab === 'assistant' && (
          <div style={{ background: '#141A29', borderRadius: 12, padding: '1.5rem' }}>
            <div style={{ height: 400, overflowY: 'auto', display: 'flex', flexDirection: 'column', gap: '1rem', marginBottom: '1rem' }}>
              {chatMessages.map((m, idx) => (
                <div key={idx} style={{ alignSelf: m.role === 'user' ? 'flex-end' : 'flex-start', background: m.role === 'user' ? '#1E293B' : '#0E131F', padding: '0.75rem 1rem', borderRadius: 8, maxWidth: '75%' }}>
                  <div>{m.content}</div>
                  {m.toolCalls?.map((tc, tIdx) => (
                    <div key={tIdx} style={{ fontSize: '0.75rem', color: '#8B5CF6', marginTop: 4 }}>🛠️ {tc.name}({JSON.stringify(tc.arguments)})</div>
                  ))}
                  {m.citedSkus?.length > 0 && (
                    <div style={{ fontSize: '0.75rem', color: '#00F0FF', marginTop: 4 }}>Cited: {m.citedSkus.join(', ')}</div>
                  )}
                </div>
              ))}
            </div>
            <div style={{ display: 'flex', gap: '0.5rem' }}>
              <input value={chatInput} onChange={e => setChatInput(e.target.value)} onKeyDown={e => e.key === 'Enter' && sendChat()} placeholder="Ask parts, stock, or draft orders..." style={{ flex: 1, padding: '0.75rem', background: '#0E131F', border: '1px solid #1E283D', borderRadius: 8, color: '#fff' }} />
              <button onClick={() => sendChat()} style={{ background: '#8B5CF6', color: '#fff', border: 'none', padding: '0.75rem 1.5rem', borderRadius: 8, fontWeight: 700, cursor: 'pointer' }}>Send</button>
            </div>
          </div>
        )}

        {tab === 'reorder' && (
          <div>
            <h2>Reorder Suggestions (40 Forecast SKUs)</h2>
            <table style={{ width: '100%', borderCollapse: 'collapse', textAlign: 'left' }}>
              <thead>
                <tr style={{ borderBottom: '1px solid #1E283D' }}><th>SKU</th><th>Stock</th><th>Lead Time</th><th>Demand</th><th>Safety Stock</th><th>ROP</th><th>Reorder Qty</th><th>Action</th></tr>
              </thead>
              <tbody>
                {reorders.map(r => (
                  <tr key={r.sku} style={{ borderBottom: '1px solid #1E283D' }}>
                    <td style={{ color: '#00F0FF', fontWeight: 700 }}>{r.sku}</td>
                    <td>{r.current_stock}</td>
                    <td>{r.lead_time_weeks} wks</td>
                    <td>{r.lead_time_demand}</td>
                    <td>{r.safety_stock}</td>
                    <td>{r.reorder_point}</td>
                    <td style={{ fontWeight: 800, color: r.reorder_qty > 0 ? '#F59E0B' : '#9CA3AF' }}>{r.reorder_qty}</td>
                    <td>
                      {r.reorder_qty > 0 && <button onClick={() => addToCart({ sku: r.sku, name: r.sku, price: 500 }, r.reorder_qty)} style={{ background: 'transparent', border: '1px solid #00F0FF', color: '#00F0FF', padding: '0.2rem 0.5rem', borderRadius: 4, cursor: 'pointer' }}>+ Cart</button>}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </main>
    </div>
  );
}
