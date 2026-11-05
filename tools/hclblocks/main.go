package main

import (
	"encoding/json"
	"fmt"
	"io"
	"os"

	"github.com/hashicorp/hcl/v2"
	"github.com/hashicorp/hcl/v2/hclsyntax"
)

type Range struct {
	Filename string `json:"filename"`
	Start    Pos    `json:"start"`
	End      Pos    `json:"end"`
}

type Pos struct {
	Line   int `json:"line"`
	Column int `json:"column"`
	Byte   int `json:"byte"`
}

type BlockInfo struct {
	Type   string      `json:"type"`
	Labels []string    `json:"labels"`
	Range  Range       `json:"range"`
	Blocks []BlockInfo `json:"blocks,omitempty"`
}

type ParseResult struct {
	Valid  bool        `json:"valid"`
	Error  string      `json:"error,omitempty"`
	Blocks []BlockInfo `json:"blocks"`
}

func extractBlocks(body *hclsyntax.Body) []BlockInfo {
	if body == nil {
		return nil
	}
	res := []BlockInfo{}
	for _, b := range body.Blocks {
		r := b.Range()
		bi := BlockInfo{
			Type:   b.Type,
			Labels: b.Labels,
			Range: Range{
				Filename: r.Filename,
				Start:    Pos{Line: r.Start.Line, Column: r.Start.Column, Byte: r.Start.Byte},
				End:      Pos{Line: r.End.Line, Column: r.End.Column, Byte: r.End.Byte},
			},
			Blocks: extractBlocks(b.Body),
		}
		res = append(res, bi)
	}
	return res
}

func main() {
	src, err := io.ReadAll(os.Stdin)
	if err != nil {
		out, _ := json.Marshal(ParseResult{Valid: false, Error: err.Error()})
		fmt.Println(string(out))
		os.Exit(1)
	}

	file, diags := hclsyntax.ParseConfig(src, "stdin.tf", hcl.InitialPos)
	if diags.HasErrors() {
		out, _ := json.Marshal(ParseResult{Valid: false, Error: diags.Error()})
		fmt.Println(string(out))
		os.Exit(1)
	}

	body, ok := file.Body.(*hclsyntax.Body)
	if !ok {
		out, _ := json.Marshal(ParseResult{Valid: false, Error: "not an hclsyntax.Body"})
		fmt.Println(string(out))
		os.Exit(1)
	}

	blocks := extractBlocks(body)
	out, _ := json.Marshal(ParseResult{Valid: true, Blocks: blocks})
	fmt.Println(string(out))
}
