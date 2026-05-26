import { useState, useEffect } from 'react'
import axios from 'axios'

export default function ModelManager() {
  const [models, setModels] = useState([])
  const [newModel, setNewModel] = useState('')
  const [loading, setLoading] = useState(false)
  const [statusMsg, setStatusMsg] = useState('')

  const fetchModels = async () => {
    const res = await axios.get('/api/models')
    setModels(res.data.models)
  }

  useEffect(() => { fetchModels() }, [])

  const pullModel = async () => {
    if (!newModel.trim()) return
    setLoading(true)
    setStatusMsg('')
    try {
      await axios.post('/api/models/pull', { model_name: newModel.trim() })
      setStatusMsg(`Pull initiated for ${newModel}. Refresh in a few minutes.`)
    } catch (e) {
      setStatusMsg(`Error: ${e.response?.data?.detail || e.message}`)
    } finally {
      setLoading(false)
    }
  }

  const deleteModel = async (name) => {
    if (!confirm(`Delete ${name}?`)) return
    try {
      await axios.delete(`/api/models/${encodeURIComponent(name)}`)
      fetchModels()
    } catch (e) {
      alert('Delete failed: ' + (e.response?.data?.detail || e.message))
    }
  }

  return (
    <div style={{ padding: '1rem' }}>
      <h3>Model Manager</h3>
      
      <div style={{ marginBottom: '1rem' }}>
        <input
          value={newModel}
          onChange={e => setNewModel(e.target.value)}
          placeholder="model name (e.g., llama3.1:8b)"
          style={{ width: '250px', marginRight: '0.5rem' }}
        />
        <button onClick={pullModel} disabled={loading || !newModel.trim()}>
          Pull
        </button>
      </div>

      {statusMsg && <p style={{ fontStyle: 'italic' }}>{statusMsg}</p>}

      <h4>Installed Models</h4>
      <ul>
        {models.map(m => (
          <li key={m}>
            {m}
            <button onClick={() => deleteModel(m)} style={{ marginLeft: '1rem', color: 'red' }}>
              Delete
            </button>
          </li>
        ))}
      </ul>
    </div>
  )
}
