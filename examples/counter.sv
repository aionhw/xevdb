// 8-bit synchronous counter with synchronous active-high reset.
// Used as a trudbg smoke-test target.
module counter #(
    parameter WIDTH = 8
) (
    input  logic              clk,
    input  logic              rst,
    input  logic              en,
    output logic [WIDTH-1:0]  count
);

    always_ff @(posedge clk) begin
        if (rst)
            count <= '0;
        else if (en)
            count <= count + 1'b1;
    end

endmodule

// Simple top wrapper that instantiates the counter.
module top (
    input  logic       clk,
    input  logic       rst,
    output logic [7:0] count
);
    logic en;
    assign en = 1'b1;

    counter #(.WIDTH(8)) u_cnt (
        .clk   (clk),
        .rst   (rst),
        .en    (en),
        .count (count)
    );
endmodule
