import numpy as np

import dash
from dash import dcc, html, Input, Output, State
import plotly.express as px
import plotly.graph_objects as go
import pandas as pd

from scripts import config, helper
from run_model_projection import Analyzer


app = dash.Dash(__name__)
app.title = "Habana-Viewer"


devices = list(config.HardwareParameters.keys())
types = list(config.DeviceType2Ratio.keys())
dtypes = list(config.DType2Bytes.keys())


models = list(config.ModelDict.keys())
in_min, in_max, in_step = 128, 4096, 512
out_min, out_max, out_step = 512, 8192, 512
kv_bucket_min, kv_bucket_max, kv_bucket_step = 128, 1024, 128
batch_sizes = [1]
for _ in range(9):
    batch_sizes.append(batch_sizes[-1] * 2)


height = 700


app.layout = html.Div([
    html.Div([
        html.Div([
            html.Label("Device"),
            dcc.Dropdown(id='device-dropdown',
                         options=[{'label': i, 'value': i} for i in devices], value=devices[0]),

            html.Label("Type"),
            dcc.Dropdown(
                id='type-dropdown', options=[{'label': i, 'value': i} for i in types], value=types[0]),

            html.Label("Dtype"),
            dcc.Dropdown(
                id='dtype-dropdown', options=[{'label': i, 'value': i} for i in dtypes], value=dtypes[-2]),
        ], style={'width': '30%', 'display': 'inline-block'}),

        html.Div([
            html.Label("Model"),
            dcc.Dropdown(
                id='model-dropdown', options=[{'label': i, 'value': i} for i in models], value=models[1]),

            html.Label("Input Length"),
            dcc.Slider(id='input-length-slider', min=in_min,
                       max=in_max, step=in_step, value=in_min),

            html.Label("Output Length"),
            dcc.Slider(id='output-length-slider', min=out_min,
                       max=out_max, step=out_step, value=out_max/2),

            html.Label("Batch Size"),
            dcc.Dropdown(id='batch-size-dropdown', options=[
                         {'label': i, 'value': i} for i in batch_sizes], multi=True, value=batch_sizes)
        ], style={'width': '30%', 'display': 'inline-block', 'marginLeft': '5%'}),

        html.Div([
            html.Label("KVcache_Bucket"),
            dcc.Slider(id='kvcache-bucket-slider', min=kv_bucket_min,
                       max=kv_bucket_max, step=kv_bucket_step, value=128)
        ], style={'width': '30%', 'display': 'inline-block', 'marginLeft': '5%'}),

        html.Button('Run Analysis', id='run-analysis-button'),
    ], style={'padding': 20}),

    html.Div([
        dcc.Graph(id='overall-projection-graph'),
        dcc.Graph(id='layer-projection-graph')
        # html.Div([
        #     dcc.Graph(id='overall-projection-graph')
        # ], style={'width': '49%', 'display': 'inline-block'}),

        # html.Div([
        #     dcc.Graph(id='layer-projection-graph')
        # ], style={'width': '49%', 'display': 'inline-block', 'float': 'right'})
    ], style={'padding': 20}),

    html.Div([
        html.Label("Overall Projection Table"),
        html.Div(id='overall-projection-table')
    ], style={'padding': 20}),

    dcc.Interval(id='interval-component', interval=1 * \
                 1000, n_intervals=0, max_intervals=1)
])


def plot_overall_projection(device, type_, model, dtype, kvcache_bucket, overall_projection):
    batch_sizes = overall_projection["batch_sizes"]
    batch_seqlens = overall_projection["batch_seqlens"]
    batch_seqlens = [[seqlens[0]] + [kvcache_bucket] *
                     (len(seqlens)-1) for seqlens in batch_seqlens]
    batch_latencies = overall_projection["batch_latencies"]
    batch_throughputs = overall_projection["batch_throughputs"]

    fig_overall = go.Figure()

    for i, batch_size in enumerate(batch_sizes):
        # px.colors.qualitative.G10 / px.colors.sequential.Viridis
        color_scale = px.colors.sequential.Plasma
        max_latency = max(batch_latencies[i])
        min_latency = min(batch_latencies[i])
        color_step = (max_latency - min_latency) / len(color_scale)

        for j, seq_len in enumerate(batch_seqlens[i]):
            accum_seq_len = batch_seqlens[i][0] if j == 0 else sum(
                batch_seqlens[i][:j])
            latency = batch_latencies[i][j]
            color_index = int((latency - min_latency) / color_step)
            batch_color = color_scale[color_index % len(color_scale)]

            fig_overall.add_trace(go.Bar(
                x=[batch_size],
                y=[seq_len],
                base=accum_seq_len,
                width=0.5,
                name=f'T={accum_seq_len}',
                marker=dict(color=batch_color),
                hovertemplate=f'Tokens: {accum_seq_len}<br>Latency: {latency} ms',
                text=latency
            ))

    fig_overall.update_layout(
        barmode='stack',
        xaxis=dict(
            title='Batch Size',
            type='category',
            tickmode='array',
            tickvals=batch_sizes,
            ticktext=batch_sizes
        ),
        yaxis=dict(
            title='Sequence Length',
            type='linear'
        ),
        yaxis2=dict(
            # title='Throughput (tokens/s)',
            overlaying='y',
            side='right'
        ),
        title=f"{device}{type_}_{model}_{dtype}_overall_projection",
        height=height,
        margin=dict(r=300)
    )

    fig_overall.add_trace(go.Scatter(
        x=batch_sizes,
        y=batch_throughputs,
        mode='lines+markers',
        name='Throughput (tokens/s)',
        yaxis='y2',
        marker=dict(color='orangered')
    ))

    return fig_overall


def plot_layer_projection(device, type_, model, dtype, layer_projection):
    cfg = config.HardwareConfig(device, type_, dtype)

    peak_bandwidth = cfg.hbm_bandwidth / config.TFLOPS
    operational_intensity = np.logspace(-3, 3, 2048)

    peak_flops_mme = cfg.flops_mme / config.T_BW
    turning_point_mme = peak_flops_mme / peak_bandwidth
    attainable_tops_mme = peak_flops_mme
    roofline_mme = np.minimum(
        peak_flops_mme, peak_bandwidth * operational_intensity)

    peak_flops_vec = cfg.flops_vec / config.T_BW
    turning_point_vec = peak_flops_vec / peak_bandwidth
    attainable_tops_vec = peak_flops_vec
    roofline_vec = np.minimum(
        peak_flops_vec, peak_bandwidth * operational_intensity)

    fig_layer = go.Figure()
    # MME
    fig_layer.add_trace(go.Scatter(
        x=operational_intensity, y=roofline_mme, mode='lines',
        name=f'{device}{type_} MME Roofline (TFLOPs)'
    ))
    fig_layer.add_shape(
        type="line",
        x0=np.min(operational_intensity), y0=np.max(roofline_mme),
        x1=turning_point_mme, y1=np.max(roofline_mme),
        line=dict(color="red", width=1, dash="dash"),
    )
    fig_layer.add_shape(
        type="line",
        x0=turning_point_mme, y0=np.min(roofline_mme),
        x1=turning_point_mme, y1=np.max(roofline_mme),
        line=dict(color="red", width=1, dash="dash"),
    )
    fig_layer.add_trace(go.Scatter(
        x=[turning_point_mme], y=[np.max(roofline_mme)],
        mode='markers+text', name='MME Truning Point',
        text=["MME"], textposition="bottom right",
        marker=dict(color='red', size=8)
    ))
    # VEC
    fig_layer.add_trace(go.Scatter(
        x=operational_intensity, y=roofline_vec, mode='lines',
        name=f'{device}{type_} VEC Roofline (TFLOPs)'
    ))
    fig_layer.add_shape(
        type="line",
        x0=np.min(operational_intensity), y0=np.max(roofline_vec),
        x1=turning_point_vec, y1=np.max(roofline_vec),
        line=dict(color="red", width=1, dash="dash"),
    )
    fig_layer.add_shape(
        type="line",
        x0=turning_point_vec, y0=np.min(roofline_vec),
        x1=turning_point_vec, y1=np.max(roofline_vec),
        line=dict(color="red", width=1, dash="dash"),
    )
    fig_layer.add_trace(go.Scatter(
        x=[turning_point_vec], y=[np.max(roofline_vec)],
        mode='markers+text', name='VEC Truning Point',
        text=["VEC"], textposition="bottom right",
        marker=dict(color='red', size=8)
    ))
    fig_layer.update_layout(
        xaxis=dict(type='log', title='Arithmetic Intensity (FLOP:Byte)'),
        yaxis=dict(type='log', title='Performance (TFLOPs/sec)'),
        title=f"{device}{type_}_{model}_{dtype}_layer_projection",
        height=height
    )

    marker_shapes = {
        "qkvo_proj": "circle",
        "qk_matmul": "square",
        "softmax": "diamond",
        "sv_matmul": "cross",
        "up_proj": "x",
        "down_proj": "triangle-up",
        "gate_proj": "triangle-down"
    }

    batch_sizes = layer_projection["batch_sizes"]
    batch_layer_projection = layer_projection["batch_layer_projection"]
    base_color = px.colors.sequential.Plasma  # Viridis

    for i, batch_size in enumerate(batch_sizes):
        layer_prefill = batch_layer_projection[i]["prefill"]
        layer_decode = batch_layer_projection[i]["decode"]
        color_prefill = base_color[(i+1) % len(base_color)]
        color_decode = base_color[(i+2) % len(base_color)]

        # prefill (first step)
        for op, proj in layer_prefill.items():
            if proj is not None:
                ai, tops = proj
                fig_layer.add_trace(go.Scatter(
                    x=[ai],
                    y=[tops],
                    mode='markers',
                    name=f'bs{batch_size}_{op}_prefill',
                    text=[
                        f"OP: {op}<br>Phase: prefill<br>AI: {ai:.2f}<br>Tops: {tops:.2f}TFLOPs"],
                    textposition="top left",
                    marker=dict(
                        color=color_prefill,
                        symbol=marker_shapes.get(op, 'circle'),
                        size=8
                    )
                ))
        # decode (last step)
        for op, proj in layer_decode.items():
            if proj is not None:
                ai, tops = proj
                fig_layer.add_trace(go.Scatter(
                    x=[ai],
                    y=[tops],
                    mode='markers',
                    name=f'bs{batch_size}_{op}_decode',
                    text=[
                        f"OP: {op}<br>Phase: decode<br>AI: {ai:.2f}<br>Tops: {tops:.2f}TFLOPs"],
                    textposition="top left",
                    marker=dict(
                        color=color_decode,
                        symbol=marker_shapes.get(op, 'circle'),
                        size=8
                    )
                ))

    return fig_layer


@app.callback(
    [Output('overall-projection-graph', 'figure'),
     Output('layer-projection-graph', 'figure'),
     Output('overall-projection-table', 'children')],
    [Input('run-analysis-button', 'n_clicks'),
     Input('interval-component', 'n_intervals')],
    [State('device-dropdown', 'value'),
     State('type-dropdown', 'value'),
     State('dtype-dropdown', 'value'),
     State('model-dropdown', 'value'),
     State('input-length-slider', 'value'),
     State('output-length-slider', 'value'),
     State('batch-size-dropdown', 'value')],
    [State('kvcache-bucket-slider', 'value')]
)
def update_output(n_clicks, n_intervals, device, type_, dtype, model, input_length, output_length, batch_sizes, kvcache_bucket):
    if not n_clicks and n_intervals == 0:
        return dash.no_update, dash.no_update, dash.no_update

    proj_cfg = {
        "device_list": [device],
        "type_list": [type_],
        "dtype_list": [dtype],
        "model_list": [model],
        "parallel": {
            "pp_list": [1],
            "tp_list": [1],
        },
        "context": {
            "input_list": [input_length],
            "output_list": [output_length],
        },
        "bs_list": batch_sizes,
        "optims": {
            "kvcache_bucket": kvcache_bucket,
            "enable_vec_bmm": True,
        }
    }

    analyzer = Analyzer(proj_cfg)
    proj_dict = analyzer.analyze(True)[model]
    overall_projection, overall_projection_table = helper.extract_overall_projection(
        proj_dict, device, type_, 1, 1, dtype, input_length, output_length, kvcache_bucket, batch_sizes)
    layer_projection = helper.extract_layer_projection(
        proj_dict, device, type_, 1, 1, dtype, input_length, output_length, kvcache_bucket, batch_sizes)

    fig_overall = plot_overall_projection(
        device, type_, model, dtype, kvcache_bucket, overall_projection)
    fig_layer = plot_layer_projection(
        device, type_, model, dtype, layer_projection)

    # table_overall = dash.dash_table.DataTable(
    #     columns=[{"name": i, "id": i} for i in overall_projection_table.columns],
    #     data=overall_projection_table.to_dict('records')
    # )
    table_overall = None

    return fig_overall, fig_layer, table_overall


if __name__ == '__main__':
    app.run_server(debug=False)
